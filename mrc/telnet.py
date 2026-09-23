#!/usr/bin/env python3
#
# vim: tabstop=4 expandtab shiftwidth=4 softtabstop=4
#
"""Telnet debug shell: an exec() REPL against a caller-supplied namespace."""

import contextlib
import sys
from io import StringIO
from typing import Optional, Union

import telnetlib3
from telnetlib3.stream_reader import TelnetReader, TelnetReaderUnicode
from telnetlib3.stream_writer import TelnetWriter, TelnetWriterUnicode

TelnetReaderType = Union[TelnetReader, TelnetReaderUnicode]
TelnetWriterType = Union[TelnetWriter, TelnetWriterUnicode]


@contextlib.contextmanager
def stdoutIO(stdout: Optional[StringIO] = None):
    old = sys.stdout
    if stdout is None:
        stdout = StringIO()
    sys.stdout = stdout
    yield stdout
    sys.stdout = old


async def start(port: int, namespace: dict):
    """Start the telnet debug shell on `port`, exec()'ing commands against `namespace`."""

    async def shell(reader: TelnetReaderType, writer: TelnetWriterType) -> None:
        """
        A default telnet shell, appropriate for use with telnetlib3.create_server.

        This shell provides a very simple REPL, allowing introspection and state
        toggling of the connected client session.
        """
        CR = telnetlib3.server_shell.CR
        LF = telnetlib3.server_shell.LF
        writer.write("Ready." + CR + LF)

        linereader = telnetlib3.server_shell.readline(reader, writer)
        linereader.send(None)

        command = None
        while True:
            if command:
                writer.write(CR + LF)
            writer.write("tel:sh> ")
            command = None
            while command is None:
                await writer.drain()
                inp = await reader.read(1)
                if not inp:
                    return
                command = linereader.send(inp)
            writer.write(CR + LF)
            if command == "quit":
                writer.write("Goodbye." + CR + LF)
                break
            else:
                with stdoutIO() as s:
                    try:
                        exec(command, namespace)
                    except Exception as e:
                        writer.write('{}\n'.format(e))
                    writer.write('{}\n'.format(s.getvalue()))

        writer.close()

    return await telnetlib3.create_server(port=port, shell=shell)
