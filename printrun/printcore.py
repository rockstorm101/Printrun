# This file is part of the Printrun suite.
#
# Printrun is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Printrun is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Printrun.  If not, see <http://www.gnu.org/licenses/>.

import threading
import queue
import io
import time
import logging
import sys
import serial

class Printcore:
    """Dumb gcode sender.

    It is "dumb" in the sense that it does not interpret neither the
    commands nor the output received from the machine. It simply sends
    commands and reports back the output (if any).

    It is "machine agnostic". It can be used to connect to any kind of
    CNC machine such as milling machines, 3D printers or laser
    cutters.

    Attributes
    ----------
    check_interval : float
        Time interval in seconds to check for responses from the
        machine. The lower this time, the more CPU intensive
        `Printcore` will be. (Default is 0.1)
    on_command_sent
        Placeholder for a callback function that will be called every
        time a command is sent to the machine. This function will be
        called with a sinlge argument: a string containing the command
        sent.
    on_feedback_received
        Placeholder for a callback function that will be called every
        time feedback is gathered from the machine. This function will
        be called with one argument: a string containing a single line
        received from the machine. If multiple lines are received,
        this function will be called as many times, once per line.
    on_job_end
        Placeholder for a callback function that will be called
        whenever a job is finished. This function will be called with
        no arguments.

    """

    def __init__(self):
        self.check_interval = 0.1
        self.on_command_sent = None
        self.on_feedback_received = None
        self.on_job_end = None

        # A queue of commands that will be sent ahead of the standard
        # command queue
        self._priority_command_queue = queue.Queue()
        # A queue of commands to be gradually sent to the
        # machine. `maxsize` is set to keep memory consumption at a
        # minimum. This queue will gradually be filled and emptied
        self._command_queue = queue.Queue(maxsize = 5)
        # A queue that holds the outputs received from the machine
        self._report_queue = queue.Queue()
        # Signal that there are commands awaiting to be sent in any of
        # the command queues
        self._command_queue_not_empty = threading.Event()
        # Signal that the standard command queue is not full, thus it
        # is ready to accept more commands to be loaded onto it
        self._command_queue_not_full = threading.Event()
        # Signal that the command sending process is no longer
        # paused. It has to be "not_paused" instead of the more
        # intuitive "paused" to be able to wait for resume
        self._not_paused = threading.Event()
        # Signal that the machine has acknowledged the command sent to it
        self._command_acknowledged = threading.Event()
        # Placeholder to hold the connection to machine's port or
        # network socket
        self._machine = None
        # Whether `Printcore` is connected to the machine
        self._connected = False
        # Whether a job has been started
        self._working = False
        # Whether to return or not some feedback from the machine
        self._report_feedback = False

        logging.debug('constructor: initiated a Printcore instance')

    def connect(self, port, baudrate, file=sys.stdout):
        """Establishes a connection to the machine.

        Parameters
        ----------
        port : str
            Either a device name, such as '/dev/ttyUSB0' or 'COM3', or
            an URL.
        baudrate : int
            Baud rate such as 9600 or 115200.
        file : Text I/O, optional
            Either a file or a file-like stream. Any feedback received
            from the machine will be written to it. (Default is STDOUT)

        Raises
        ------
        ConnectionError
            If an error occurred when attempting to connect

        """

        if self._connected:
            return

        # Create and connect to either a socket or a serial port
        try:
            self._machine = serial.serial_for_url(url = port,
                                                  baudrate = baudrate)
        except serial.SerialException as e:
            raise ConnectionError(e.strerror)

        self._connected = True
        logging.info('Connected to %s at baudrate %d', port, baudrate)

        # Create and start the sender thread
        sender_thread = threading.Thread(name = 'sender-thread',
                                         target = self._sender)
        sender_thread.start()

        # Create and start the listener thread
        listener_thread = threading.Thread(name = 'listener-thread',
                                           target = self._listener,
                                           args = (file,))
        listener_thread.start()

    def is_connected(self):
        """Returns True if `Printcore` is connected to a machine."""

        return self._connected

    def disconnect(self):
        """Terminates the connection to the machine"""

        if not self._connected:
            return
        self._connected = False

        if self._working:
            self.cancel()

        # If sender thread is waiting for new commands,
        # command-queue-not-empty is signalled so it stops waiting and
        # detects the disconnection
        if not self._command_queue_not_empty.is_set():
            self._command_queue_not_empty.set()
            logging.debug('disconnect: signalled command-queue-not-empty')
        # If sender thread is waiting for a command acknowledgement,
        # command-acknowledged is signalled so it stops waiting and
        # detects the disconnection
        if not self._command_acknowledged.is_set():
            self._command_acknowledged.set()
            logging.debug('disconnect: signalled command-acknowledged')

        # Wait for the listener thread to recheck for connection
        time.sleep(self.check_interval)

        self._machine.close()
        logging.info('Disconnected from %s', self._machine.port)

    def send_now(self, command):
        """Sends a single command ahead of the command queue.

	Parameters
        ----------
        command : str
            Command to be sent to the machine.
         
        Returns
        -------
        str
            String containing the feedback received from the machine
	    after sending the command.

        """

        logging.debug('send-now: treating command: "%s"', command)
        parsed_command = self._parse_command(command)
        if parsed_command is not None:
            self._load_now(parsed_command)
            logging.debug('send-now: waiting for feedback')
            report = self._report_queue.get()
            logging.debug('send-now: got "%s" reported', str(report))
        return report

    def start(self, file=sys.stdin):
        """Gradually reads and sends commands to the machine.
        
        It starts a process or job that will run in the
        background. This process reads commands one by one from the
        given input stream and sends them to the machine.

        Only one job can be processed at a time. If called when
        another job is running it will do nothing.

        Parameters
        ----------
        file : Text I/O
            Either a file or a file-like stream from where the
            commands are read. (Default is STDIN)

        """

        # Check whether another work is running
        if self._working:
            logging.warning('Another work is already running!')
            return

        self._working = True
        self._lines_read = 0
        logging.info('Starting work...')

        self._not_paused.set()
        logging.debug('start: cleared paused signal')

        # Create and start loader thread
        loader_thread = threading.Thread(name = 'loader-thread',
                                         target = self._loader,
                                         args = (file,))
        loader_thread.start()
        self._command_queue_not_full.set()

    def is_working(self):
        """Returns True if a job is being processed."""

        return self._working

    def lines_read(self):
        """Returns the number of lines read by the current job."""

        return self._lines_read
    
    def pause(self):
        """Pause the ongoing job."""

        logging.info('Pausing work...')
        if self._not_paused.is_set(): # if paused == False
            self._not_paused.clear()  #     paused = True
            logging.debug('pause: signal paused')

    def is_paused(self):
        """Returns True if a job is paused."""

        return not self._not_paused.is_set() # return paused
            
    def resume(self):
        """Resume the (previously paused) job."""

        logging.info('Resuming work...')
        if not self._not_paused.is_set(): # if paused == True
            self._not_paused.set()        #     paused = False
            logging.debug('resume: clear paused signal')

        # if sender thread was waiting for new commands, signal
        # command-queue-not-empty so it resumes sending
        if not self._command_queue_not_empty.is_set():
            self._command_queue_not_empty.set()
            logging.debug('resume: signalled command-queue-not-empty')

    def cancel(self):
        """Terminates the ongoing job."""

        if not self._working:
            return
        logging.info('Cancelling work...')
        self._working = False

        # if the job was paused, the loader thread might be waiting for
        # resume. Thus not-paused is signalled so it detects the job
        # cancellation
        if not self._not_paused.is_set(): # if paused == True
            self._not_paused.set()
            logging.debug('cancel: paused signal cleared')
        # if the loader thread is waiting for a slot in the command
        # queue, signal there is a slot so it detects the job
        # cancellation
        if not self._command_queue_not_full.is_set():
            self._command_queue_not_full.set()
            logging.debug('cancel: command-queue-not-full signalled')
        self._flush_command_queue()

    def _loader(self, file):
        # loader-thread
        # This thread gradually reads lines from 'file', extract
        # commands, and loads them onto the command queue.
        for line in file:
            logging.debug('loader: read line: "%s"', line)
            command = self._parse_command(line)
            if command is None:
                continue

            while self._connected and self._working:
                logging.debug('loader: waiting for a slot in command queue')
                self._command_queue_not_full.wait()
                logging.debug('loader: detected command-queue-not-full')
                if not self._not_paused.is_set(): # if paused == True
                    logging.debug('loader: detected job pause, waiting')
                    self._not_paused.wait()
                    logging.debug('loader: detected resume')
                elif self._command_queue.full():
                    logging.debug('loader: command queue full')
                    self._command_queue_not_full.clear()
                    logging.debug('loader: cleared command-queue-not-full')
                    continue
                else:
                    self._load(command)
                    break
            else:
                # if cancelled or disconnected -> exit the for loop
                logging.debug('loader: job cancellation detected')
                return


        logging.debug('loader: reached end of file')
        # Call job end callback function
        if self.on_job_end:
            self.on_job_end()

    def _load_now(self, command):
        self._priority_command_queue.put(command)
        logging.debug('now-load: loaded priority command: "%s"', str(command))
        if not self._command_queue_not_empty.is_set():
            self._command_queue_not_empty.set()
            logging.debug('now-load: signalled command-queue-not-empty')

    def _load(self, command):
        self._command_queue.put(command)
        logging.debug('load: loaded command "%s"', str(command))
        if not self._command_queue_not_empty.is_set():
            self._command_queue_not_empty.set()
            logging.debug('load: signalled command-queue-not-empty')

    def _sender(self):
        while self._connected:
            # wait for new commands
            logging.debug('sender: waiting for new commands')
            self._command_queue_not_empty.wait()
            logging.debug('sender: received command-queue-not-empty signal')

            # expect a new command-acknowledged signal after sending
            if self._command_acknowledged.is_set():
                self._command_acknowledged.clear()
                logging.debug('sender: command-acknowledged cleared')

            # query the command queues for commands
            if not self._priority_command_queue.empty():
                logging.debug('sender: priority command queue not empty')
                command = self._priority_command_queue.get()
                self._report_feedback = True
                logging.debug('sender: got command "%s"', str(command))
            elif (self._working
                  and self._not_paused.is_set() # paused = False
                  and not self._command_queue.empty()):
                logging.debug('sender: command queue not empty')
                command = self._command_queue.get()
                self._report_feedback = False
                logging.debug('sender: got command "%s"', command.code)
                if not self._command_queue_not_full.is_set():
                    self._command_queue_not_full.set()
                    logging.debug(
                        'sender: signalled command-queue-not-full')
            else:
                logging.debug('sender: found no commands to be sent')
                self._command_queue_not_empty.clear()
                logging.debug('sender: cleared command-queue-not-empty')
                continue

            self._send(command)

            # wait for command acknowledgement
            logging.debug('sender: waiting for acknowledgement')
            self._command_acknowledged.wait()
            logging.debug('sender: command acknowledged')
        else:
            # if disconnected -> end thread
            logging.debug('sender: detected disconnection')
            return

    def _send(self, command):
        # Sends the command to the machine 
        logging.debug('send: sending command "%s" to machine', command.code)
        code = command.code
        encoded_code = (code + '\n').encode()
        self._machine.write(encoded_code)
        logging.debug('send: sent command "%s"', code)

        # If a function is set for sent commands, call it
        if self.on_command_sent:
            self.on_command_sent(code)

    def _listener(self, file):
        while self._connected:
            feedback = self._listen()
            if feedback is not None:
                self._parse_feedback(feedback)
                file.write(feedback)
            else:
                #FIXME: ugly hack, please improve this
                time.sleep(self.check_interval) 
                continue
        else:
            # if disconnected -> end thread
            logging.debug('listener: detected disconnection')
            return

    def _listen(self):
        if self._machine.in_waiting:
            logging.debug("listen: detected feedback from the machine")
            encoded_feedback = self._machine.readline()
            feedback = encoded_feedback.decode()
            logging.debug("listen: received %s", feedback)

            # If a function is set for received feedback, call it
            if self.on_feedback_received:
                self.on_feedback_received(feedback)

            return feedback
        else:
            return None

        
    def _flush_command_queue(self):
        while not self._command_queue.empty():
            self._command_queue.get()

    def _parse_command(self, line):
        # extract the gcode from a line
        # returns an instance of the _Command class or None
        logging.debug('command-parser: parsing "%s"', line)
        code = line.split(';', 1)[0]  # strip ;-style comments
        code = code.split('(', 1)[0]  # strip ()-style comments
        code = code.rstrip()          # strip trailing spaces and newlines
        if code == "":
            logging.debug('command_parser: no command code found')
            return None
        else:
            command = _Command(code)
            logging.debug('command-parser: parsed into code "%s"', code)
            return command

    def _parse_feedback(self, feedback):
        logging.debug('feedback-parser: parsing "%s"', feedback)
        if feedback.startswith('ok'):
            logging.debug('feedback-parser: detected command acknowledgement')
            if self._report_feedback == True:
                report = self._parse_report(feedback)
                logging.debug('feedback-parser: reporting %s"', feedback)
                self._report_queue.put(report)
                logging.debug('feedback-parser: reported')
            if not self._command_acknowledged.is_set():
                self._command_acknowledged.set()
                logging.debug('feedback-parser: command-acknowledged signalled')
        elif feedback.lower().startswith('resend'):
            logging.debug('feedback-parser: detected command resend request')

    def _parse_report(self, feedback):
       report = feedback.strip('\n')
       return report
            
class _Command:
    def __init__(self, code, id = None):
        self.code = code     # (str)
        self.checksum = None # (int)  checksum?
        self.id = id         # (int)  id?

    def __str__(self):
        return self.code
