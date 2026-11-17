"""Byte transports to a hub: serial (USB or UART adapter) and TCP. Standard library only (Linux)."""

import fcntl
import os
import select
import socket
import struct
import termios


class Transport:
    name = "?"
    has_baud = False  # only a real UART needs the 2 Mbaud switch after FRM1
    ack_timeout_ms = 30  # a cable answers in a few ms

    def open(self): ...
    def close(self): ...
    def write(self, data: bytes): ...

    def read(self, timeout: float) -> bytes:
        """Whatever arrived within timeout seconds; b"" if nothing. ConnectionError when the link is gone."""
        ...

    def set_baud(self, baud: int): ...


class SerialTransport(Transport):
    """A tty. Opening does not toggle DTR/RTS, so an ESP32's auto-reset circuit is left alone."""

    def __init__(self, path: str, uart: bool = False):
        self.path = path
        self.name = path
        self.has_baud = uart
        self.fd = None

    def open(self):
        self.fd = os.open(self.path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        attrs = termios.tcgetattr(self.fd)
        iflag, oflag, cflag, lflag, ispeed, ospeed, cc = attrs
        iflag = 0
        oflag = 0
        lflag = 0
        cflag = (cflag & ~(termios.CSIZE | termios.PARENB | termios.CSTOPB | termios.CRTSCTS | termios.HUPCL)) \
            | termios.CS8 | termios.CREAD | termios.CLOCAL
        cc[termios.VMIN] = 0
        cc[termios.VTIME] = 0
        termios.tcsetattr(self.fd, termios.TCSANOW, [iflag, oflag, cflag, lflag, termios.B115200, termios.B115200, cc])
        # assert DTR and RTS together: on ESP32 USB Serial/JTAG and the usual 2-transistor circuit that is "run"
        bits = struct.pack("I", termios.TIOCM_DTR | termios.TIOCM_RTS)
        fcntl.ioctl(self.fd, termios.TIOCMBIS, bits)
        termios.tcflush(self.fd, termios.TCIFLUSH)

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def write(self, data: bytes):
        view = memoryview(data)
        while view:
            try:
                n = os.write(self.fd, view)
            except BlockingIOError:
                select.select([], [self.fd], [], 0.1)
                continue
            view = view[n:]

    def read(self, timeout: float) -> bytes:
        r, _, _ = select.select([self.fd], [], [], timeout)
        if not r:
            return b""
        try:
            data = os.read(self.fd, 4096)
        except BlockingIOError:
            return b""
        except OSError as e:
            raise ConnectionError(str(e)) from e
        if not data:
            raise ConnectionError("serial port closed")
        return data

    def set_baud(self, baud: int):
        if not self.has_baud:
            return
        speed = getattr(termios, f"B{baud}")
        attrs = termios.tcgetattr(self.fd)
        attrs[4] = attrs[5] = speed
        termios.tcsetattr(self.fd, termios.TCSADRAIN, attrs)
        termios.tcflush(self.fd, termios.TCIFLUSH)


class TcpTransport(Transport):
    ack_timeout_ms = 1000  # Wi-Fi: answers take up to several hundred ms
    def __init__(self, host: str, port: int = 6639):
        self.host, self.port = host, port
        self.name = f"{host}:{port}"
        self.sock = None

    def open(self):
        self.sock = socket.create_connection((self.host, self.port), timeout=5)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        self.sock.settimeout(None)

    def close(self):
        if self.sock is not None:
            try:
                self.sock.close()
            finally:
                self.sock = None

    def write(self, data: bytes):
        sock = self.sock
        if sock is None:
            raise ConnectionError("not connected")
        try:
            sock.sendall(data)
        except OSError as e:
            raise ConnectionError(str(e)) from e

    def read(self, timeout: float) -> bytes:
        sock = self.sock  # close() from another thread sets self.sock to None
        if sock is None:
            raise ConnectionError("not connected")
        try:
            r, _, _ = select.select([sock], [], [], timeout)
            if not r:
                return b""
            data = sock.recv(4096)
        except (OSError, ValueError) as e:
            raise ConnectionError(str(e)) from e
        if not data:
            raise ConnectionError("connection closed by the hub")
        return data


def open_transport(spec: str) -> Transport:
    """'/dev/ttyACM0' (native USB), 'uart:/dev/ttyUSB0' (UART adapter), or 'host[:port]' (TCP)."""
    if spec.startswith("uart:"):
        return SerialTransport(spec[5:], uart=True)
    if spec.startswith("/dev/"):
        return SerialTransport(spec)
    host, _, port = spec.rpartition(":") if ":" in spec else (spec, "", "")
    return TcpTransport(host or spec, int(port) if port else 6639)
