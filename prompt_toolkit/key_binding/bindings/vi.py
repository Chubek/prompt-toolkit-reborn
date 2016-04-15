# pylint: disable=function-redefined
from __future__ import unicode_literals
from prompt_toolkit.buffer import ClipboardData, indent, unindent, reshape_text
from prompt_toolkit.document import Document
from prompt_toolkit.enums import IncrementalSearchDirection, SEARCH_BUFFER, SYSTEM_BUFFER
from prompt_toolkit.filters import Filter, Condition, HasArg, Always, to_cli_filter, IsReadOnly, ViMode
from prompt_toolkit.key_binding.vi_state import CharacterFind, InputMode
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout.utils import find_window_for_buffer_name
from prompt_toolkit.selection import SelectionType

from .utils import create_handle_decorator
from .scroll import scroll_forward, scroll_backward, scroll_half_page_up, scroll_half_page_down, scroll_one_line_up, scroll_one_line_down, scroll_page_up, scroll_page_down

import prompt_toolkit.filters as filters
import codecs
import six

__all__ = (
    'load_vi_bindings',
    'load_vi_search_bindings',
    'load_vi_system_bindings',
    'load_extra_vi_page_navigation_bindings',
)


class TextObjectType(object):
    EXCLUSIVE = 'EXCLUSIVE'
    INCLUSIVE = 'INCLUSIVE'
    LINEWISE = 'LINEWISE'


class TextObject(object):
    """
    Return struct for functions wrapped in ``text_object``.
    Both `start` and `end` are relative to the current cursor position.
    """
    def __init__(self, start, end=0, type=TextObjectType.EXCLUSIVE):
        self.start = start
        self.end = end
        self.type = type

    @property
    def selection_type(self):
        if self.type == TextObjectType.LINEWISE:
            return SelectionType.LINES
        else:
            return SelectionType.CHARACTERS

    def sorted(self):
        """
        Return a (start, end) tuple where start <= end.
        """
        if self.start < self.end:
            return self.start, self.end
        else:
            return self.end, self.start

    def operator_range(self, document):
        """
        Return a (start, end) tuple with start <= end that indicates the range
        operators should operate on.
        `buffer` is used to get start and end of line positions.
        """
        start, end = self.sorted()
        doc = document

        if (self.type == TextObjectType.EXCLUSIVE and
                doc.translate_index_to_position(end + doc.cursor_position)[1] == 0):
            # If the motion is exclusive and the end of motion is on the first
            # column, the end position becomes end of previous line.
            end -= 1
        if self.type == TextObjectType.INCLUSIVE:
            end += 1
        if self.type == TextObjectType.LINEWISE:
            # Select whole lines
            row, col = doc.translate_index_to_position(start + doc.cursor_position)
            start = doc.translate_row_col_to_index(row, 0) - doc.cursor_position
            row, col = doc.translate_index_to_position(end + doc.cursor_position)
            end = doc.translate_row_col_to_index(row, len(doc.lines[row])) - doc.cursor_position
        return start, end

    def get_line_numbers(self, buffer):
        """
        Return a (start_line, end_line) pair.
        """
        # Get absolute cursor positions from the text object.
        from_, to = self.operator_range(buffer.document)
        from_ += buffer.cursor_position
        to += buffer.cursor_position

        # Take the start of the lines.
        from_, _ = buffer.document.translate_index_to_position(from_)
        to, _ = buffer.document.translate_index_to_position(to)

        return from_, to


def load_vi_bindings(registry, enable_visual_key=Always(), get_search_state=None, filter=None):
    """
    Vi extensions.

    # Overview of Readline Vi commands:
    # http://www.catonmat.net/download/bash-vi-editing-mode-cheat-sheet.pdf

    :param enable_visual_key: Filter to enable lowercase 'v' bindings. A reason
        to disable these are to support open-in-editor functionality. These key
        bindings conflict.
     :param get_search_state: None or a callable that takes a
        CommandLineInterface and returns a SearchState.
    """
    # Note: Some key bindings have the "~IsReadOnly()" filter added. This
    #       prevents the handler to be executed when the focus is on a
    #       read-only buffer.
    #       This is however only required for those that change the ViState to
    #       INSERT mode. The `Buffer` class itself throws the
    #       `EditReadOnlyBuffer` exception for any text operations which is
    #       handled correctly. There is no need to add "~IsReadOnly" to all key
    #       bindings that do text manipulation.

    enable_visual_key = to_cli_filter(enable_visual_key)

    # Default get_search_state.
    if get_search_state is None:
        def get_search_state(cli): return cli.search_state

    handle = create_handle_decorator(registry, filter)

    # (Note: Always take the navigation bindings in read-only mode, even when
    #  ViState says different.)
    navigation_mode = (ViMode(InputMode.NAVIGATION) | IsReadOnly()) & ~ filters.HasSelection()
    insert_mode = ViMode(InputMode.INSERT) & ~ filters.HasSelection() & ~IsReadOnly()
    replace_mode = ViMode(InputMode.REPLACE) & ~ filters.HasSelection() & ~IsReadOnly()
    selection_mode = filters.HasSelection()
    operator_given = Condition(lambda cli: cli.vi_state.operator_func is not None)

    vi_transform_functions = [
        # Rot 13 transformation
        (('g', '?'), lambda string: codecs.encode(string, 'rot_13')),

        # To lowercase
        (('g', 'u'), lambda string: string.lower()),

        # To uppercase.
        (('g', 'U'), lambda string: string.upper()),

        # Swap case.
        # (XXX: If we would implement 'tildeop', the 'g' prefix is not required.)
        (('g', '~'), lambda string: string.swapcase()),
    ]

    def check_cursor_position(event):
        """
        After every command, make sure that if we are in navigation mode, we
        never put the cursor after the last character of a line. (Unless it's
        an empty line.)
        """
        buff = event.current_buffer
        preferred_column = buff.preferred_column

        if (
                (filter is None or filter(event.cli)) and  # First make sure that this key bindings are active.

                event.cli.vi_state.input_mode == InputMode.NAVIGATION and
                buff.document.is_cursor_at_the_end_of_line and
                len(buff.document.current_line) > 0):
            buff.cursor_position -= 1

            # Set the preferred_column for arrow up/down again.
            # (This was cleared after changing the cursor position.)
            buff.preferred_column = preferred_column

    registry.on_handler_called += check_cursor_position

    @handle(Keys.Escape)
    def _(event):
        """
        Escape goes to vi navigation mode.
        """
        buffer = event.current_buffer
        vi_state = event.cli.vi_state

        if vi_state.input_mode in (InputMode.INSERT, InputMode.REPLACE):
            buffer.cursor_position += buffer.document.get_cursor_left_position()

        vi_state.input_mode = InputMode.NAVIGATION

        if bool(buffer.selection_state):
            buffer.exit_selection()

    @handle('k', filter=selection_mode)
    def _(event):
        """
        Arrow up in selection mode.
        """
        event.current_buffer.cursor_up(count=event.arg)

    @handle('j', filter=selection_mode)
    def _(event):
        """
        Arrow down in selection mode.
        """
        event.current_buffer.cursor_down(count=event.arg)

    @handle('k', filter=navigation_mode)
    @handle(Keys.Up, filter=navigation_mode)
    @handle(Keys.ControlP, filter=navigation_mode)
    def _(event):
        """
        Arrow up and ControlP in navigation mode go up.
        """
        b = event.current_buffer
        b.auto_up(count=event.arg)

    @handle('j', filter=navigation_mode)
    @handle(Keys.Down, filter=navigation_mode)
    @handle(Keys.ControlN, filter=navigation_mode)
    def _(event):
        """
        Arrow down and Control-N in navigation mode.
        """
        b = event.current_buffer
        b.auto_down(count=event.arg)

    @handle(Keys.Backspace, filter=navigation_mode)
    def _(event):
        """
        In navigation-mode, move cursor.
        """
        event.current_buffer.cursor_position += \
            event.current_buffer.document.get_cursor_left_position(count=event.arg)

    @handle(Keys.ControlV, Keys.Any, filter=insert_mode)
    def _(event):
        """
        Insert a character literally (quoted insert).
        """
        event.current_buffer.insert_text(event.data, overwrite=False)

    @handle(Keys.ControlN, filter=insert_mode)
    def _(event):
        b = event.current_buffer

        if b.complete_state:
            b.complete_next()
        else:
            event.cli.start_completion(select_first=True)

    @handle(Keys.ControlP, filter=insert_mode)
    def _(event):
        """
        Control-P: To previous completion.
        """
        b = event.current_buffer

        if b.complete_state:
            b.complete_previous()
        else:
            event.cli.start_completion(select_last=True)

    @handle(Keys.ControlY, filter=insert_mode)
    def _(event):
        """
        Accept current completion.
        """
        event.current_buffer.complete_state = None

    @handle(Keys.ControlE, filter=insert_mode)
    def _(event):
        """
        Cancel completion. Go back to originally typed text.
        """
        event.current_buffer.cancel_completion()

    @handle(Keys.ControlJ, filter=navigation_mode)   # XXX: only if the selected buffer has a return handler.
    def _(event):
        """
        In navigation mode, pressing enter will always return the input.
        """
        b = event.current_buffer

        if b.accept_action.is_returnable:
            b.accept_action.validate_and_handle(event.cli, b)

    # ** In navigation mode **

    # List of navigation commands: http://hea-www.harvard.edu/~fine/Tech/vi.html

    @handle(Keys.Insert, filter=navigation_mode)
    def _(event):
        " Presing the Insert key. "
        event.cli.vi_state.input_mode = InputMode.INSERT

    @handle('a', filter=navigation_mode & ~IsReadOnly())
            # ~IsReadOnly, because we want to stay in navigation mode for
            # read-only buffers.
    def _(event):
        event.current_buffer.cursor_position += event.current_buffer.document.get_cursor_right_position()
        event.cli.vi_state.input_mode = InputMode.INSERT

    @handle('A', filter=navigation_mode & ~IsReadOnly())
    def _(event):
        event.current_buffer.cursor_position += event.current_buffer.document.get_end_of_line_position()
        event.cli.vi_state.input_mode = InputMode.INSERT

    @handle('C', filter=navigation_mode & ~IsReadOnly())
    def _(event):
        """
        # Change to end of line.
        # Same as 'c$' (which is implemented elsewhere.)
        """
        buffer = event.current_buffer

        deleted = buffer.delete(count=buffer.document.get_end_of_line_position())
        event.cli.clipboard.set_text(deleted)
        event.cli.vi_state.input_mode = InputMode.INSERT

    @handle('c', 'c', filter=navigation_mode & ~IsReadOnly())
    @handle('S', filter=navigation_mode & ~IsReadOnly())
    def _(event):  # TODO: implement 'arg'
        """
        Change current line
        """
        buffer = event.current_buffer

        # We copy the whole line.
        data = ClipboardData(buffer.document.current_line, SelectionType.LINES)
        event.cli.clipboard.set_data(data)

        # But we delete after the whitespace
        buffer.cursor_position += buffer.document.get_start_of_line_position(after_whitespace=True)
        buffer.delete(count=buffer.document.get_end_of_line_position())
        event.cli.vi_state.input_mode = InputMode.INSERT

    @handle('D', filter=navigation_mode)
    def _(event):
        buffer = event.current_buffer
        deleted = buffer.delete(count=buffer.document.get_end_of_line_position())
        event.cli.clipboard.set_text(deleted)

    @handle('d', 'd', filter=navigation_mode)
    def _(event):
        """
        Delete line. (Or the following 'n' lines.)
        """
        buffer = event.current_buffer

        # Split string in before/deleted/after text.
        lines = buffer.document.lines

        before = '\n'.join(lines[:buffer.document.cursor_position_row])
        deleted = '\n'.join(lines[buffer.document.cursor_position_row: buffer.document.cursor_position_row + event.arg])
        after = '\n'.join(lines[buffer.document.cursor_position_row + event.arg:])

        # Set new text.
        if before and after:
            before = before + '\n'

        # Set text and cursor position.
        buffer.document = Document(
            text=before + after,
            # Cursor At the start of the first 'after' line, after the leading whitespace.
            cursor_position = len(before) + len(after) - len(after.lstrip(' ')))

        # Set clipboard data
        event.cli.clipboard.set_data(ClipboardData(deleted, SelectionType.LINES))

    @handle('i', filter=navigation_mode & ~IsReadOnly())
    def _(event):
        event.cli.vi_state.input_mode = InputMode.INSERT

    @handle('I', filter=navigation_mode & ~IsReadOnly())
    def _(event):
        event.cli.vi_state.input_mode = InputMode.INSERT
        event.current_buffer.cursor_position += \
            event.current_buffer.document.get_start_of_line_position(after_whitespace=True)

    @handle('J', filter=navigation_mode & ~IsReadOnly())
    def _(event):
        """ Join lines. """
        for i in range(event.arg):
            event.current_buffer.join_next_line()

    @handle('J', filter=selection_mode & ~IsReadOnly())
    def _(event):
        """ Join selected lines. """
        event.current_buffer.join_selected_lines()

    @handle('n', filter=navigation_mode)
    def _(event):  # XXX: use `text_object`
        """
        Search next.
        """
        event.current_buffer.apply_search(
            get_search_state(event.cli), include_current_position=False,
            count=event.arg)

    @handle('N', filter=navigation_mode)
    def _(event):  # TODO: use `text_object`
        """
        Search previous.
        """
        event.current_buffer.apply_search(
            ~get_search_state(event.cli), include_current_position=False,
            count=event.arg)

    @handle('p', filter=navigation_mode)
    def _(event):
        """
        Paste after
        """
        event.current_buffer.paste_clipboard_data(
            event.cli.clipboard.get_data(),
            count=event.arg)

    @handle('P', filter=navigation_mode)
    def _(event):
        """
        Paste before
        """
        event.current_buffer.paste_clipboard_data(
            event.cli.clipboard.get_data(),
            before=True,
            count=event.arg)

    @handle('r', Keys.Any, filter=navigation_mode)
    def _(event):
        """
        Replace single character under cursor
        """
        event.current_buffer.insert_text(event.data * event.arg, overwrite=True)
        event.current_buffer.cursor_position -= 1

    @handle('R', filter=navigation_mode)
    def _(event):
        """
        Go to 'replace'-mode.
        """
        event.cli.vi_state.input_mode = InputMode.REPLACE

    @handle('s', filter=navigation_mode & ~IsReadOnly())
    def _(event):
        """
        Substitute with new text
        (Delete character(s) and go to insert mode.)
        """
        text = event.current_buffer.delete(count=event.arg)
        event.cli.clipboard.set_text(text)
        event.cli.vi_state.input_mode = InputMode.INSERT

    @handle('u', filter=navigation_mode, save_before=(lambda e: False))
    def _(event):
        for i in range(event.arg):
            event.current_buffer.undo()

    @handle('V', filter=navigation_mode)
    def _(event):
        """
        Start lines selection.
        """
        event.current_buffer.start_selection(selection_type=SelectionType.LINES)

    @handle(Keys.ControlV, filter=navigation_mode)
    def _(event):
        " Enter block selection mode. "
        event.current_buffer.start_selection(selection_type=SelectionType.BLOCK)

    @handle('V', filter=selection_mode)
    def _(event):
        """
        Exit line selection mode, or go from non line selection mode to line
        selection mode.
        """
        selection_state = event.current_buffer.selection_state

        if selection_state.type != SelectionType.LINES:
            selection_state.type = SelectionType.LINES
        else:
            event.current_buffer.exit_selection()

    @handle('v', filter=navigation_mode & enable_visual_key)
    def _(event):
        " Enter character selection mode. "
        event.current_buffer.start_selection(selection_type=SelectionType.CHARACTERS)

    @handle('v', filter=selection_mode)
    def _(event):
        """
        Exit character selection mode, or go from non-character-selection mode
        to character selection mode.
        """
        selection_state = event.current_buffer.selection_state

        if selection_state.type != SelectionType.CHARACTERS:
            selection_state.type = SelectionType.CHARACTERS
        else:
            event.current_buffer.exit_selection()

    @handle(Keys.ControlV, filter=selection_mode)
    def _(event):
        """
        Exit block selection mode, or go from non block selection mode to block
        selection mode.
        """
        selection_state = event.current_buffer.selection_state

        if selection_state.type != SelectionType.BLOCK:
            selection_state.type = SelectionType.BLOCK
        else:
            event.current_buffer.exit_selection()


    @handle('a', 'w', filter=selection_mode)
    @handle('a', 'W', filter=selection_mode)
    def _(event):
        """
        Switch from visual linewise mode to visual characterwise mode.
        """
        buffer = event.current_buffer

        if buffer.selection_state and buffer.selection_state.type == SelectionType.LINES:
            buffer.selection_state.type = SelectionType.CHARACTERS

    @handle('x', filter=navigation_mode)
    def _(event):
        """
        Delete character.
        """
        text = event.current_buffer.delete(count=event.arg)
        event.cli.clipboard.set_text(text)

    @handle('x', filter=selection_mode)
    @handle('d', filter=selection_mode)
    def _(event):
        """
        Cut selection.
        """
        clipboard_data = event.current_buffer.cut_selection()
        event.cli.clipboard.set_data(clipboard_data)

    @handle('c', filter=selection_mode & ~IsReadOnly())
    def _(event):
        """
        Change selection (cut and go to insert mode).
        """
        clipboard_data = event.current_buffer.cut_selection()
        event.cli.clipboard.set_data(clipboard_data)
        event.cli.vi_state.input_mode = InputMode.INSERT

    @handle('y', filter=selection_mode)
    def _(event):
        """
        Copy selection.
        """
        clipboard_data = event.current_buffer.copy_selection()
        event.cli.clipboard.set_data(clipboard_data)

    @handle('X', filter=navigation_mode)
    def _(event):
        text = event.current_buffer.delete_before_cursor()
        event.cli.clipboard.set_text(text)

    @handle('y', 'y', filter=navigation_mode)
    @handle('Y', filter=navigation_mode)
    def _(event):
        """
        Yank the whole line.
        """
        text = '\n'.join(event.current_buffer.document.lines_from_current[:event.arg])
        event.cli.clipboard.set_data(ClipboardData(text, SelectionType.LINES))

    @handle('+', filter=navigation_mode)
    def _(event):
        """
        Move to first non whitespace of next line
        """
        buffer = event.current_buffer
        buffer.cursor_position += buffer.document.get_cursor_down_position(count=event.arg)
        buffer.cursor_position += buffer.document.get_start_of_line_position(after_whitespace=True)

    @handle('-', filter=navigation_mode)
    def _(event):
        """
        Move to first non whitespace of previous line
        """
        buffer = event.current_buffer
        buffer.cursor_position += buffer.document.get_cursor_up_position(count=event.arg)
        buffer.cursor_position += buffer.document.get_start_of_line_position(after_whitespace=True)

    @handle('>', '>', filter=navigation_mode)
    def _(event):
        """
        Indent lines.
        """
        buffer = event.current_buffer
        current_row = buffer.document.cursor_position_row
        indent(buffer, current_row, current_row + event.arg)

    @handle('<', '<', filter=navigation_mode)
    def _(event):
        """
        Unindent lines.
        """
        current_row = event.current_buffer.document.cursor_position_row
        unindent(event.current_buffer, current_row, current_row + event.arg)

    @handle('O', filter=navigation_mode & ~IsReadOnly())
    def _(event):
        """
        Open line above and enter insertion mode
        """
        event.current_buffer.insert_line_above(
                copy_margin=not event.cli.in_paste_mode)
        event.cli.vi_state.input_mode = InputMode.INSERT

    @handle('o', filter=navigation_mode & ~IsReadOnly())
    def _(event):
        """
        Open line below and enter insertion mode
        """
        event.current_buffer.insert_line_below(
                copy_margin=not event.cli.in_paste_mode)
        event.cli.vi_state.input_mode = InputMode.INSERT

    @handle('~', filter=navigation_mode)
    def _(event):
        """
        Reverse case of current character and move cursor forward.
        """
        buffer = event.current_buffer
        c = buffer.document.current_char

        if c is not None and c != '\n':
            c = (c.upper() if c.islower() else c.lower())
            buffer.insert_text(c, overwrite=True)

    @handle('#', filter=navigation_mode)
    def _(event):
        """
        Go to previous occurence of this word.
        """
        b = event.cli.current_buffer

        search_state = get_search_state(event.cli)
        search_state.text = b.document.get_word_under_cursor()
        search_state.direction = IncrementalSearchDirection.BACKWARD

        b.apply_search(search_state, count=event.arg,
                       include_current_position=False)

    @handle('*', filter=navigation_mode)
    def _(event):
        """
        Go to next occurence of this word.
        """
        b = event.cli.current_buffer

        search_state = get_search_state(event.cli)
        search_state.text = b.document.get_word_under_cursor()
        search_state.direction = IncrementalSearchDirection.FORWARD

        b.apply_search(search_state, count=event.arg,
                       include_current_position=False)

    @handle('(', filter=navigation_mode)
    def _(event):
        # TODO: go to begin of sentence.
        # XXX: should become text_object.
        pass

    @handle(')', filter=navigation_mode)
    def _(event):
        # TODO: go to end of sentence.
        # XXX: should become text_object.
        pass

    def operator(*keys, **kw):
        """
        Register a Vi operator.

        Usage::

            @operator('d', filter=...)
            def handler(cli, text_object):
                # Do something with the text object here.
        """
        filter = kw.pop('filter', Always())
        assert not kw

        def decorator(operator_func):
            @handle(*keys, filter=~operator_given & filter & navigation_mode)
            def _(event):
                """
                Handle operator in navigation mode.
                """
                # When this key binding is matched, only set the operator
                # function in the ViState. We should execute it after a text
                # object has been received.
                event.cli.vi_state.operator_func = operator_func
                event.cli.vi_state.operator_arg = event.arg

            @handle(*keys, filter=~operator_given & filter & selection_mode)
            def _(event):
                """
                Handle operator in selection mode.
                """
                buff = event.current_buffer
                selection_state = buff.selection_state

                # Create text object from selection.
                if selection_state.type == SelectionType.LINES:
                    text_obj_type = TextObjectType.LINEWISE
                else:
                    text_obj_type = TextObjectType.INCLUSIVE

                    # TODO: handle block selections in the operators.

                text_object = TextObject(
                    selection_state.original_cursor_position - buff.cursor_position,
                    type=text_obj_type)

                # Execute operator.
                operator_func(event, text_object)

                # Quit selection mode.
                buff.selection_state = None

        return decorator

    def text_object(*keys, filter=Always(), no_move_handler=False):
        """
        Register a text object function.

        Usage::

            @text_object('w', filter=...)
            def handler(event):
                # Return a text object for this key.
                return TextObject(...)

        :param no_move_handler: Disable the move handler in navigation mode.
            (It's still active in selection mode.)
        """
        def decorator(text_object_func):
            assert callable(text_object_func)

            @handle(*keys, filter=operator_given & filter)
            def _(event):
                # Call the text object handler.
                text_obj = text_object_func(event)
                if text_obj is not None:
                    assert isinstance(text_obj, TextObject)

                    # Call the operator function with the text object.
                    event.cli.vi_state.operator_func(event, text_obj)

                # Clear operator.
                event.cli.vi_state.operator_func = None
                event.cli.vi_state.operator_arg = None

            # Register a move operation. (Doesn't need an operator.)
            if not no_move_handler:
                @handle(*keys, filter=~operator_given & filter & navigation_mode)
                def _(event):
                    " Move handler for navigation mode. "
                    text_object = text_object_func(event)
                    event.current_buffer.cursor_position += text_object.start

            @handle(*keys, filter=~operator_given & filter & selection_mode)
            def _(event):
                " Move handler for selection mode. "
                text_object = text_object_func(event)
                buff = event.current_buffer

                # When the text object has both a start and end position, like 'i(' or 'iw',
                # Turn this into a selection, otherwise the cursor.
                if text_object.end:
                    # Take selection positions from text object.
                    start, end = text_object.operator_range(buff.document)
                    start += buff.cursor_position
                    end += buff.cursor_position

                    buff.selection_state.original_cursor_position = start
                    buff.cursor_position = end

                    # Take selection type from text object.
                    if text_object.type == TextObjectType.LINEWISE:
                        buff.selection_state.type = SelectionType.LINES
                    else:
                        buff.selection_state.type = SelectionType.CHARACTERS
                else:
                    event.current_buffer.cursor_position += text_object.start

            # Make it possible to chain @text_object decorators.
            return text_object_func

        return decorator

    @text_object(Keys.Any, filter=operator_given)
    def _(event):
        """
        Unknown key binding while waiting for a text object.
        """
        event.cli.output.bell()

    #
    # *** Operators ***
    #

    def create_delete_and_change_operators(delete_only):
        """
        Create delete and change handlers.
        """
        @operator('cd'[delete_only], filter=~IsReadOnly())
        def delete_operator(event, text_object):
            deleted = ''
            buff = event.current_buffer

            if text_object:  # XXX: is this possible?
                start, end = text_object.operator_range(buff.document)

                # Move to the start of the text_object.
                buff.cursor_position += start

                # Delete until end of text_object.
                deleted = buff.delete(count=end-start)

            # Set deleted/changed text to clipboard.
            if deleted:
                event.cli.clipboard.set_data(ClipboardData(deleted, text_object.selection_type))

            # If using 'd' operator with a linewise motion, delete
            # the newline as well.
            if delete_only and text_object.type == TextObjectType.LINEWISE:
                buff.delete() or buff.delete_before_cursor()

            # Only go back to insert mode in case of 'change'.
            if not delete_only:
                event.cli.vi_state.input_mode = InputMode.INSERT

    create_delete_and_change_operators(False)
    create_delete_and_change_operators(True)

    def create_transform_handler(transform_func, *a):
        @operator(*a, filter=~IsReadOnly())
        def _(event, text_object):
            """
            Apply transformation (uppercase, lowercase, rot13, swap case).
            """
            buff = event.current_buffer
            start, end = text_object.operator_range(buff.document)

            if start < end:
                # Transform.
                buff.transform_region(
                    buff.cursor_position + start,
                    buff.cursor_position + end,
                    transform_func)

                # Move cursor
                buff.cursor_position += (text_object.end or text_object.start)

    for k, f in vi_transform_functions:
        create_transform_handler(f, *k)

    @operator('y')
    def yank_handler(event, text_object):
        """
        Yank operator. (Copy text.)
        """
        buff = event.current_buffer

        start, end = text_object.operator_range(buff.document)
        substring = buff.text[buff.cursor_position + start: buff.cursor_position + end]

        if substring:
            event.cli.clipboard.set_data(ClipboardData(substring, text_object.selection_type))

    @operator('>')
    def _(event, text_object):
        """
        Indent.
        """
        buff = event.current_buffer
        from_, to = text_object.get_line_numbers(buff)
        indent(buff, from_, to + 1, count=event.arg)

    @operator('<')
    def _(event, text_object):
        """
        Unindent.
        """
        buff = event.current_buffer
        from_, to = text_object.get_line_numbers(buff)
        unindent(buff, from_, to + 1, count=event.arg)

    @operator('g', 'q')
    def _(event, text_object):
        """
        Reshape text.
        """
        buff = event.current_buffer
        from_, to = text_object.get_line_numbers(buff)
        reshape_text(buff, from_, to)

    # TODO: Also "gq": text formatting

    #
    # *** Text objects ***
    #

    @text_object('b')
    def _(event):
        """ Move one word or token left. """
        return TextObject(event.current_buffer.document.find_start_of_previous_word(count=event.arg) or 0)

    @text_object('B')
    def _(event):
        """ Move one non-blank word left """
        return TextObject(event.current_buffer.document.find_start_of_previous_word(count=event.arg, WORD=True) or 0)

    @text_object('$')
    def key_dollar(event):
        """ 'c$', 'd$' and '$':  Delete/change/move until end of line. """
        return TextObject(event.current_buffer.document.get_end_of_line_position())

    @text_object('w')
    def _(event):
        """ 'word' forward. 'cw', 'dw', 'w': Delete/change/move one word.  """
        return TextObject(event.current_buffer.document.find_next_word_beginning(count=event.arg) or
                            event.current_buffer.document.get_end_of_document_position())

    @text_object('W')
    def _(event):
        """ 'WORD' forward. 'cW', 'dW', 'W': Delete/change/move one WORD.  """
        return TextObject(event.current_buffer.document.find_next_word_beginning(count=event.arg, WORD=True) or
                            event.current_buffer.document.get_end_of_document_position())

    @text_object('e')
    def _(event):
        """ End of 'word': 'ce', 'de', 'e' """
        end = event.current_buffer.document.find_next_word_ending(count=event.arg)
        return TextObject(end - 1 if end else 0, type=TextObjectType.INCLUSIVE)

    @text_object('E')
    def _(event):
        """ End of 'WORD': 'cE', 'dE', 'E' """
        end = event.current_buffer.document.find_next_word_ending(count=event.arg, WORD=True)
        return TextObject(end - 1 if end else 0, type=TextObjectType.INCLUSIVE)

    @text_object('i', 'w', no_move_handler=True)
    def _(event):
        """ Inner 'word': ciw and diw """
        start, end = event.current_buffer.document.find_boundaries_of_current_word()
        return TextObject(start, end)

    @text_object('a', 'w', no_move_handler=True)
    def _(event):
        """ A 'word': caw and daw """
        start, end = event.current_buffer.document.find_boundaries_of_current_word(include_trailing_whitespace=True)
        return TextObject(start, end)

    @text_object('i', 'W', no_move_handler=True)
    def _(event):
        """ Inner 'WORD': ciW and diW """
        start, end = event.current_buffer.document.find_boundaries_of_current_word(WORD=True)
        return TextObject(start, end)

    @text_object('a', 'W', no_move_handler=True)
    def _(event):
        """ A 'WORD': caw and daw """
        start, end = event.current_buffer.document.find_boundaries_of_current_word(WORD=True, include_trailing_whitespace=True)
        return TextObject(start, end)

    @text_object('a', 'p', no_move_handler=True)
    def _(event):
        """
        Auto paragraph.
        """
        start = event.current_buffer.document.start_of_paragraph()
        end = event.current_buffer.document.end_of_paragraph(count=event.arg)
        return TextObject(start, end)

    @text_object('^')
    def key_circumflex(event):
        """ 'c^', 'd^' and '^': Soft start of line, after whitespace. """
        return TextObject(event.current_buffer.document.get_start_of_line_position(after_whitespace=True))

    @text_object('0', no_move_handler=True)
    def key_zero(event):
        """
        'c0', 'd0': Hard start of line, before whitespace.
        (The move '0' key is implemented elsewhere, because a '0' could also change the `arg`.)
        """
        return TextObject(event.current_buffer.document.get_start_of_line_position(after_whitespace=False))

    def create_ci_ca_handles(ci_start, ci_end, inner):
                # TODO: 'dab', 'dib', (brackets or block) 'daB', 'diB', Braces.
                # TODO: 'dat', 'dit', (tags (like xml)
        """
        Delete/Change string between this start and stop character. But keep these characters.
        This implements all the ci", ci<, ci{, ci(, di", di<, ca", ca<, ... combinations.
        """
        @text_object('ai'[inner], ci_start, no_move_handler=True)
        @text_object('ai'[inner], ci_end, no_move_handler=True)
        def _(event):
            if ci_start == ci_end:
                # Quotes
                start = event.current_buffer.document.find_backwards(ci_start, in_current_line=False)
                end = event.current_buffer.document.find(ci_end, in_current_line=False)
            else:
                # Brackets
                start = event.current_buffer.document.find_enclosing_bracket_left(ci_start, ci_end)
                end = event.current_buffer.document.find_enclosing_bracket_right(ci_start, ci_end)

            if start is not None and end is not None:
                offset = 0 if inner else 1
                return TextObject(start + 1 - offset, end + offset)
            else:
                # Nothing found.
                return TextObject(0)

    for inner in (False, True):
        for ci_start, ci_end in [('"', '"'), ("'", "'"), ("`", "`"),
                                 ('[', ']'), ('<', '>'), ('{', '}'), ('(', ')')]:
            create_ci_ca_handles(ci_start, ci_end, inner)

    @text_object('{')
    def _(event):
        """
        Move to previous blank-line separated section.
        Implements '{', 'c{', 'd{', 'y{'
        """
        index = event.current_buffer.document.start_of_paragraph(count=event.arg, before=True)
        return TextObject(index)

    @text_object('}')
    def _(event):
        """
        Move to next blank-line separated section.
        Implements '}', 'c}', 'd}', 'y}'
        """
        index = event.current_buffer.document.end_of_paragraph(count=event.arg, after=True)
        return TextObject(index)

    @text_object('f', Keys.Any)
    def _(event):
        """
        Go to next occurance of character. Typing 'fx' will move the
        cursor to the next occurance of character. 'x'.
        """
        event.cli.vi_state.last_character_find = CharacterFind(event.data, False)
        match = event.current_buffer.document.find(event.data, in_current_line=True, count=event.arg)
        if match:
            return TextObject(match, type=TextObjectType.INCLUSIVE)
        else:
            return TextObject(0)

    @text_object('F', Keys.Any)
    def _(event):
        """
        Go to previous occurance of character. Typing 'Fx' will move the
        cursor to the previous occurance of character. 'x'.
        """
        event.cli.vi_state.last_character_find = CharacterFind(event.data, True)
        return TextObject(event.current_buffer.document.find_backwards(event.data, in_current_line=True, count=event.arg) or 0)

    @text_object('t', Keys.Any)
    def _(event):
        """
        Move right to the next occurance of c, then one char backward.
        """
        event.cli.vi_state.last_character_find = CharacterFind(event.data, False)
        match = event.current_buffer.document.find(event.data, in_current_line=True, count=event.arg)
        if match:
            return TextObject(match - 1, type=TextObjectType.INCLUSIVE)
        else:
            return TextObject(0)

    @text_object('T', Keys.Any)
    def _(event):
        """
        Move left to the previous occurance of c, then one char forward.
        """
        event.cli.vi_state.last_character_find = CharacterFind(event.data, True)
        match = event.current_buffer.document.find_backwards(event.data, in_current_line=True, count=event.arg)
        return TextObject(match + 1 if match else 0)

    def repeat(reverse):
        """
        Create ',' and ';' commands.
        """
        @text_object(',' if reverse else ';')
        def _(event):
            # Repeat the last 'f'/'F'/'t'/'T' command.
            pos = 0
            vi_state = event.cli.vi_state

            type = TextObjectType.EXCLUSIVE

            if vi_state.last_character_find:
                char = vi_state.last_character_find.character
                backwards = vi_state.last_character_find.backwards

                if reverse:
                    backwards = not backwards

                if backwards:
                    pos = event.current_buffer.document.find_backwards(char, in_current_line=True, count=event.arg)
                else:
                    pos = event.current_buffer.document.find(char, in_current_line=True, count=event.arg)
                    type = TextObjectType.INCLUSIVE
            if pos:
                return TextObject(pos, type=type)
            else:
                return TextObject(0)
    repeat(True)
    repeat(False)

    @text_object('h')
    @text_object(Keys.Left)
    def _(event):
        """ Implements 'ch', 'dh', 'h': Cursor left. """
        return TextObject(event.current_buffer.document.get_cursor_left_position(count=event.arg))

    @text_object('j', no_move_handler=True)
    def _(event):
        """ Implements 'cj', 'dj', 'j', ... Cursor up. """
        return TextObject(event.current_buffer.document.get_cursor_down_position(count=event.arg), type=TextObjectType.LINEWISE)

    @text_object('k', no_move_handler=True)
    def _(event):
        """ Implements 'ck', 'dk', 'k', ... Cursor up. """
        return TextObject(event.current_buffer.document.get_cursor_up_position(count=event.arg), type=TextObjectType.LINEWISE)

    @text_object('l')
    @text_object(' ')
    @text_object(Keys.Right)
    def _(event):
        """ Implements 'cl', 'dl', 'l', 'c ', 'd ', ' '. Cursor right. """
        return TextObject(event.current_buffer.document.get_cursor_right_position(count=event.arg))

    @text_object('H')
    def _(event):
        """
        Moves to the start of the visible region. (Below the scroll offset.)
        Implements 'cH', 'dH', 'H'.
        """
        w = find_window_for_buffer_name(event.cli, event.cli.current_buffer_name)
        b = event.current_buffer

        if w and w.render_info:
            # When we find a Window that has BufferControl showing this window,
            # move to the start of the visible area.
            pos = (b.document.translate_row_col_to_index(
                       w.render_info.first_visible_line(after_scroll_offset=True), 0) -
                   b.cursor_position)

        else:
            # Otherwise, move to the start of the input.
            pos = -len(b.document.text_before_cursor)
        return TextObject(pos, type=TextObjectType.LINEWISE)

    @text_object('M')
    def _(event):
        """
        Moves cursor to the vertical center of the visible region.
        Implements 'cM', 'dM', 'M'.
        """
        w = find_window_for_buffer_name(event.cli, event.cli.current_buffer_name)
        b = event.current_buffer

        if w and w.render_info:
            # When we find a Window that has BufferControl showing this window,
            # move to the center of the visible area.
            pos = (b.document.translate_row_col_to_index(
                       w.render_info.center_visible_line(), 0) -
                   b.cursor_position)

        else:
            # Otherwise, move to the start of the input.
            pos = -len(b.document.text_before_cursor)
        return TextObject(pos, type=TextObjectType.LINEWISE)

    @text_object('L')
    def _(event):
        """
        Moves to the end of the visible region. (Above the scroll offset.)
        """
        w = find_window_for_buffer_name(event.cli, event.cli.current_buffer_name)
        b = event.current_buffer

        if w and w.render_info:
            # When we find a Window that has BufferControl showing this window,
            # move to the end of the visible area.
            pos = (b.document.translate_row_col_to_index(
                       w.render_info.last_visible_line(before_scroll_offset=True), 0) -
                   b.cursor_position)

        else:
            # Otherwise, move to the end of the input.
            pos = len(b.document.text_after_cursor)
        return TextObject(pos, type=TextObjectType.LINEWISE)

    @handle('z', '+', filter=navigation_mode|selection_mode)
    @handle('z', 't', filter=navigation_mode|selection_mode)
    @handle('z', Keys.ControlJ, filter=navigation_mode|selection_mode)
    def _(event):
        """
        Scrolls the window to makes the current line the first line in the visible region.
        """
        w = find_window_for_buffer_name(event.cli, event.cli.current_buffer_name)
        b = event.cli.current_buffer
        w.vertical_scroll = b.document.cursor_position_row

    @handle('z', '-', filter=navigation_mode|selection_mode)
    @handle('z', 'b', filter=navigation_mode|selection_mode)
    def _(event):
        """
        Scrolls the window to makes the current line the last line in the visible region.
        """
        w = find_window_for_buffer_name(event.cli, event.cli.current_buffer_name)

        # We can safely set the scroll offset to zero; the Window will meke
        # sure that it scrolls at least enough to make the cursor visible
        # again.
        w.vertical_scroll = 0

    @handle('z', 'z', filter=navigation_mode|selection_mode)
    def _(event):
        """
        Center Window vertically around cursor.
        """
        w = find_window_for_buffer_name(event.cli, event.cli.current_buffer_name)
        b = event.cli.current_buffer

        if w and w.render_info:
            info = w.render_info

            # Calculate the offset that we need in order to position the row
            # containing the cursor in the center.
            scroll_height = info.window_height // 2

            y = max(0, b.document.cursor_position_row - 1)
            height = 0
            while y > 0:
                line_height = info.get_height_for_line(y)

                if height + line_height < scroll_height:
                    height += line_height
                    y -= 1
                else:
                    break

            w.vertical_scroll = y

    @text_object('%')
    def _(event):
        """
        Implements 'c%', 'd%', '%, 'y%' (Move to corresponding bracket.)
        If an 'arg' has been given, go this this % position in the file.
        """
        buffer = event.current_buffer

        if event._arg:
            # If 'arg' has been given, the meaning of % is to go to the 'x%'
            # row in the file.
            if 0 < event.arg <= 100:
                absolute_index = buffer.document.translate_row_col_to_index(
                    int((event.arg * buffer.document.line_count - 1) / 100), 0)
                return TextObject(absolute_index - buffer.document.cursor_position, type=TextObjectType.LINEWISE)
            else:
                return TextObject(0)  # Do nothing.

        else:
            # Move to the corresponding opening/closing bracket (()'s, []'s and {}'s).
            match = buffer.document.find_matching_bracket_position()
            if match:
                return TextObject(match, type=TextObjectType.INCLUSIVE)
            else:
                return TextObject(0)

    @text_object('|')
    def _(event):
        # Move to the n-th column (you may specify the argument n by typing
        # it on number keys, for example, 20|).
        return TextObject(event.current_buffer.document.get_column_cursor_position(event.arg - 1))

    @text_object('g', 'g')
    def _(event):
        """
        Implements 'gg', 'cgg', 'ygg'
        """
        d = event.current_buffer.document

        if event._arg:
            # Move to the given line.
            return TextObject(d.translate_row_col_to_index(event.arg - 1, 0) - d.cursor_position, type=TextObjectType.LINEWISE)
        else:
            # Move to the top of the input.
            return TextObject(d.get_start_of_document_position(), type=TextObjectType.LINEWISE)

    @text_object('g', '_')
    def _(event):
        """
        Go to last non-blank of line.
        'g_', 'cg_', 'yg_', etc..
        """
        return TextObject(
            event.current_buffer.document.last_non_blank_of_current_line_position(), type=TextObjectType.INCLUSIVE)

    @text_object('g', 'e')
    def _(event):
        """
        Go to last character of previous word.
        'ge', 'cge', 'yge', etc..
        """
        prev_end = event.current_buffer.document.find_previous_word_ending(count=event.arg)
        return TextObject(prev_end - 1 if prev_end is not None else 0, type=TextObjectType.INCLUSIVE)

    @text_object('g', 'E')
    def _(event):
        """
        Go to last character of previous WORD.
        'gE', 'cgE', 'ygE', etc..
        """
        prev_end = event.current_buffer.document.find_previous_word_ending(count=event.arg, WORD=True)
        return TextObject(prev_end - 1 if prev_end is not None else 0, type=TextObjectType.INCLUSIVE)

    @text_object('G')
    def _(event):
        """
        Go to the end of the document. (If no arg has been given.)
        """
        buf = event.current_buffer
        return TextObject(buf.document.translate_row_col_to_index(buf.document.line_count - 1, 0) -
                          buf.cursor_position, type=TextObjectType.LINEWISE)

    #
    # *** Other ***
    #

    @handle('G', filter=HasArg())
    def _(event):
        """
        If an argument is given, move to this line in the  history. (for
        example, 15G)
        """
        event.current_buffer.go_to_history(event.arg - 1)

    for n in '0123456789':
        @handle(n, filter=navigation_mode|selection_mode|operator_given)
        def _(event):
            """
            Always handle numberics in navigation mode as arg.
            """
            if event.data in '123456789' or (event._arg and event.data == '0'):
                event.append_to_arg_count(event.data)
            elif event.data == '0':
                buffer = event.current_buffer
                buffer.cursor_position += buffer.document.get_start_of_line_position(after_whitespace=False)

    @handle(Keys.Any, filter=replace_mode)
    def _(event):
        """
        Insert data at cursor position.
        """
        event.current_buffer.insert_text(event.data, overwrite=True)

    @handle(Keys.ControlX, Keys.ControlL, filter=insert_mode)
    def _(event):
        """
        Pressing the ControlX - ControlL sequence in Vi mode does line
        completion based on the other lines in the document and the history.
        """
        event.current_buffer.start_history_lines_completion()

    @handle(Keys.ControlX, Keys.ControlF, filter=insert_mode)
    def _(event):
        """
        Complete file names.
        """
        # TODO
        pass


def load_vi_open_in_editor_bindings(registry, filter=None):
    """
    Pressing 'v' in navigation mode will open the buffer in an external editor.
    """
    navigation_mode = ViMode(InputMode.NAVIGATION) & ~ filters.HasSelection()
    handle = create_handle_decorator(registry, filter)

    @handle('v', filter=navigation_mode)
    def _(event):
        event.current_buffer.open_in_editor(event.cli)


def load_vi_system_bindings(registry, filter=None):
    has_focus = filters.HasFocus(SYSTEM_BUFFER)
    navigation_mode = ViMode(InputMode.NAVIGATION) & ~ filters.HasSelection()

    handle = create_handle_decorator(registry, filter)

    @handle('!', filter=~has_focus & navigation_mode)
    def _(event):
        """
        '!' opens the system prompt.
        """
        event.cli.push_focus(SYSTEM_BUFFER)
        event.cli.vi_state.input_mode = InputMode.INSERT

    @handle(Keys.Escape, filter=has_focus)
    @handle(Keys.ControlC, filter=has_focus)
    def _(event):
        """
        Cancel system prompt.
        """
        event.cli.vi_state.input_mode = InputMode.NAVIGATION
        event.cli.buffers[SYSTEM_BUFFER].reset()
        event.cli.pop_focus()

    @handle(Keys.ControlJ, filter=has_focus)
    def _(event):
        """
        Run system command.
        """
        event.cli.vi_state.input_mode = InputMode.NAVIGATION

        system_buffer = event.cli.buffers[SYSTEM_BUFFER]
        event.cli.run_system_command(system_buffer.text)
        system_buffer.reset(append_to_history=True)

        # Focus previous buffer again.
        event.cli.pop_focus()


def load_vi_search_bindings(registry, get_search_state=None,
                            filter=None, search_buffer_name=SEARCH_BUFFER):
    assert get_search_state is None or callable(get_search_state)

    if not get_search_state:
        def get_search_state(cli): return cli.search_state

    has_focus = filters.HasFocus(search_buffer_name)
    navigation_mode = ~has_focus & (ViMode(InputMode.NAVIGATION) | filters.HasSelection())
    handle = create_handle_decorator(registry, filter)

    @handle('/', filter=navigation_mode)
    @handle(Keys.ControlS, filter=~has_focus)
    def _(event):
        """
        Vi-style forward search.
        """
        # Set the ViState.
        get_search_state(event.cli).direction = IncrementalSearchDirection.FORWARD
        event.cli.vi_state.input_mode = InputMode.INSERT

        # Focus search buffer.
        event.cli.push_focus(search_buffer_name)

    @handle('?', filter=navigation_mode)
    @handle(Keys.ControlR, filter=~has_focus)
    def _(event):
        """
        Vi-style backward search.
        """
        # Set the ViState.
        get_search_state(event.cli).direction = IncrementalSearchDirection.BACKWARD

        # Focus search buffer.
        event.cli.push_focus(search_buffer_name)
        event.cli.vi_state.input_mode = InputMode.INSERT

    @handle(Keys.ControlJ, filter=has_focus)
    def _(event):
        """
        Apply the search. (At the / or ? prompt.)
        """
        input_buffer = event.cli.buffers.previous(event.cli)
        search_buffer = event.cli.buffers[search_buffer_name]

        # Update search state.
        if search_buffer.text:
            get_search_state(event.cli).text = search_buffer.text

        # Apply search.
        input_buffer.apply_search(get_search_state(event.cli))

        # Add query to history of search line.
        search_buffer.append_to_history()
        search_buffer.reset()

        # Focus previous document again.
        event.cli.vi_state.input_mode = InputMode.NAVIGATION
        event.cli.pop_focus()

    def incremental_search(cli, direction, count=1):
        " Apply search, but keep search buffer focussed. "
        # Update search_state.
        search_state = get_search_state(cli)
        direction_changed = search_state.direction != direction

        search_state.text = cli.buffers[search_buffer_name].text
        search_state.direction = direction

        # Apply search to current buffer.
        if not direction_changed:
            input_buffer = cli.buffers.previous(cli)
            input_buffer.apply_search(search_state,
                                      include_current_position=False, count=count)

    @handle(Keys.ControlR, filter=has_focus)
    def _(event):
        incremental_search(event.cli, IncrementalSearchDirection.BACKWARD, count=event.arg)

    @handle(Keys.ControlS, filter=has_focus)
    def _(event):
        incremental_search(event.cli, IncrementalSearchDirection.FORWARD, count=event.arg)

    def search_buffer_is_empty(cli):
        """ Returns True when the search buffer is empty. """
        return cli.buffers[search_buffer_name].text == ''

    @handle(Keys.Escape, filter=has_focus)
    @handle(Keys.ControlC, filter=has_focus)
    @handle(Keys.Backspace, filter=has_focus & Condition(search_buffer_is_empty))
    def _(event):
        """
        Cancel search.
        """
        event.cli.vi_state.input_mode = InputMode.NAVIGATION

        event.cli.pop_focus()
        event.cli.buffers[search_buffer_name].reset()


def load_extra_vi_page_navigation_bindings(registry, filter=None):
    """
    Key bindings, for scrolling up and down through pages.
    This are separate bindings, because GNU readline doesn't have them.
    """
    handle = create_handle_decorator(registry, filter)

    handle(Keys.ControlF)(scroll_forward)
    handle(Keys.ControlB)(scroll_backward)
    handle(Keys.ControlD)(scroll_half_page_down)
    handle(Keys.ControlU)(scroll_half_page_up)
    handle(Keys.ControlE)(scroll_one_line_down)
    handle(Keys.ControlY)(scroll_one_line_up)
    handle(Keys.PageDown)(scroll_page_down)
    handle(Keys.PageUp)(scroll_page_up)


class ViStateFilter(Filter):
    " Deprecated! "
    def __init__(self, get_vi_state, mode):
        self.mode = mode

    def __call__(self, cli):
        return cli.vi_state.input_mode == self.mode
