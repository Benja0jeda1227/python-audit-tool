"""Run the complete suite with sockets, DNS and dotenv loading forbidden."""
from pathlib import Path
import socket
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    with patch.object(socket.socket, 'connect', side_effect=AssertionError('Network forbidden')), \
         patch.object(socket.socket, 'connect_ex', side_effect=AssertionError('Network forbidden')), \
         patch('socket.getaddrinfo', side_effect=AssertionError('DNS forbidden')), \
         patch('dotenv.load_dotenv', side_effect=AssertionError('Credential file access forbidden')):
        suite = unittest.defaultTestLoader.discover(str(ROOT/'tests'))
        return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
