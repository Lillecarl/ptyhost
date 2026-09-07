import abc

__all__ = ["Backend"]


class Backend(metaclass=abc.ABCMeta):
    """
    Base class for the terminal backend-interface.
    """

    #: The process id of the program, or `None` when this backend has
    #: no number to give: a program at the other end of an ssh
    #: connection runs on another machine, and a program that has not
    #: started yet has no id.
    #:
    #: **It is here so that a reader can ask.** `pymux` read it with
    #: `getattr(backend, "pid", None)`, which answers the same for a
    #: backend that has no id and for a field somebody renamed.
    #: Lillecarl/pymux#138.
    pid = None

    def add_input_ready_callback(self, callback):
        """
        Add a new callback to be called for when there's input ready to read.
        """

    @abc.abstractmethod
    def kill(self):
        """
        Terminate the sub process.
        """

    @abc.abstractproperty
    def closed(self):
        """
        Return `True` if this is closed.
        """

    @abc.abstractmethod
    def read_text(self, amount):
        """
        Read terminal output and return it.
        """

    @abc.abstractmethod
    def write_text(self, text):
        """
        Write text to the stdin of the process.
        """

    @abc.abstractmethod
    def connect_reader(self):
        """
        Connect the reader to the event loop.
        """

    @abc.abstractmethod
    def disconnect_reader(self):
        """
        Connect the reader to the event loop.
        """

    @abc.abstractmethod
    def set_size(self, width, height):
        """
        Set terminal size.
        """

    @abc.abstractmethod
    def start(self):
        """
        Start the terminal process.
        """

    @abc.abstractmethod
    def get_name(self):
        """
        Return the name for this process, or `None` when unknown.
        """

    @abc.abstractmethod
    def get_cwd(self):
        """
        Return the current working directory of the process running in this
        terminal.
        """
