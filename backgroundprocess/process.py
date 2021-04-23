# SPDX-FileCopyrightText: (c) 2020 Artёm iG <github.com/rtmigo>
# SPDX-License-Identifier: MIT

from typing import *
import unittest

import subprocess, threading, signal
from subprocess import Popen
import time
import os

from func_timeout import func_timeout, FunctionTimedOut


class LineWaitingTimeout(Exception):
    pass


class BackgroundProcess:

    # я создал этот класс с конкретной целью: запускать HTTP-сервер в параллельном процессе, тогда как текущий
    # процесс запускает юниттесты.
    #
    # Пример использования: запускаем приложение и читаем строки, пока не встретим какую-то специальную
    #
    # with BackgroundProcess(["prog", "-arg1", "-arg2"]) as bp:
    #   for line in bp.iterLines():
    #     if something in line:
    #       break
    #
    # Важно для понимания принципа: хотя обычно поток это часть процесса, в данном случае, наоборот:
    # образно говоря, фоновый процесс Popen работает "внутри" параллельного потока Thread.
    #
    # Пока процесс не остановлен, работает и поток. Если процесс уже отработал или выкинул ошибку, сначала
    # остановится процесс, потом поток.

    def __init__(self, args: List[str], term_timeout=1, buffer_output=False, print_output=False,
                 add_env: Dict[str, str] = None):

        self._subproc: Optional[Popen] = None

        self.args = args

        self.thread = None

        self._disposed = False

        # чтобы закрыть программу, мы будем отправлять ей сигналы: вежливые и не очень.
        # После сигнала мы будем ждать столько секунд, что программа отреагирует:
        self.termTimeout: float = term_timeout

        # self.bufferOutput = self.bufferOutput
        self.buffer: Optional[List[str]] = list() if buffer_output else None
        self.printOutput = print_output

        if add_env:
            self._env = os.environ.copy()
            self._env.update((k, v) for k, v in add_env.items())
        else:
            self._env = None

    def __thread_method(self):

        self._subproc = Popen(
            self.args,
            stdout=subprocess.PIPE,
            stdin=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=self._env,
            close_fds=True)

        assert self.is_running_subprocess
        assert self.is_running_thread

    def start(self):

        if self._disposed:
            raise RuntimeError("Object disposed")

        if self.was_started:
            raise RuntimeError("Already started")

        self.thread = threading.Thread(target=self.__thread_method)
        self.thread.start()
        assert self.thread.is_alive()

    def _terminate_polite(self):

        # The SIGTERM signal is a generic signal used to cause program termination. Unlike SIGKILL, this signal can be
        # blocked, handled, and ignored. It is the normal way to politely ask a program to terminate. The shell
        # command kill generates SIGTERM by default.

        try:
            os.kill(self._waited_subproc().pid, signal.SIGTERM)
        except ProcessLookupError:  # ProcessLookupError: [Errno 3] No such process
            # похоже, процесс уже завершен
            pass

    def _terminate_brute(self):

        # я пробовал использовать этот метод, но он приносил больше проблем, чем пользы. Например, из-за
        # него сервер Flask/Werkzeug не освобождал порт и вообще не выгружался. Что странно, ведь я это делал
        # после SIGINT и SIGTERM. Но без последующего SIGKILL все закрывалось, а с SIGKILL оставался занят порт
        # и оставалось висеть приложение.
        #

        # The SIGKILL signal is used to cause immediate program termination. It cannot be handled or ignored, and is
        # therefore always fatal. It is also not possible to block this signal. # The SIGTERM signal is a generic
        # signal used to cause program termination. Unlike SIGKILL, this signal can be

        try:
            os.kill(self._waited_subproc().pid, signal.SIGKILL)
        except ProcessLookupError:  # ProcessLookupError: [Errno 3] No such process
            # похоже, процесс уже завершен
            pass

    def _terminate_ctrl_c(self):

        # The SIGINT (“program interrupt”) signal is sent when the user types the INTR character (normally Ctrl-C)
        self._waited_subproc().send_signal(signal.SIGINT)

    def terminate(self):

        if self._disposed:
            return

        if self.was_started:

            if self.is_running_subprocess:
                self._terminate_ctrl_c()
                self._terminate_polite()
                self.thread.join(timeout=self.termTimeout)

        # если join не сможет закрыть поток, никаких исключений не вылетит.
        # Узнать, что поток продолжает работать можно вызвав .is_alive()

        self.thread.join()

        self._disposed = True

    def _waited_subproc(self) -> Optional[Popen]:

        # сразу после запуска параллельного потока и фонового процесса, мы можем захотеть получить объект
        # этого процесса. Но не факт, что объект уже существует - ведь он инициализируется параллельно.

        if self._disposed:
            raise RuntimeError("Object disposed")

        if not self.was_started:
            raise RuntimeError("Was not started")

        # мы уже запустили поток
        assert self.thread is not None

        # если процесс еще не запущен - но поток работает значит процесс запускается. Ждем
        while self._subproc is None and self.thread.is_alive():
            time.sleep(50 / 1000)

        # либо _subproc инициализировался, либо поток перестал пытаться инициализировать. В любом случае,
        # уже есть значение
        return self._subproc

    def __enter__(self):

        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):

        self.terminate()

    def read_line(self) -> Optional[str]:

        # возвращает строку, как только фоновый процесс изволит ее напечатать.
        # Если процесс уже остановился, возвращает None.
        # Пока процесс ничего не печатает - метод зависает в ожидании

        if self._disposed:
            raise RuntimeError("Object disposed")

        if not self.was_started:
            raise RuntimeError("The process was not started")

        while True:

            subprocess = self._waited_subproc()
            output = subprocess.stdout.readline().decode()
            # print(output)
            if output == '':
                if not self.is_running_subprocess:
                    return None
            else:

                retline = output.strip()
                if self.buffer is not None:
                    self.buffer.append(retline)

                if self.printOutput:
                    print(retline)

                return retline

    def wait_for_line(self, predicate: Callable[[str], bool], read_timeout=None, match_timeout=None) -> bool:

        # дожидается выдачи определенной строки сервером

        start_time = time.monotonic() if match_timeout is not None else None

        while True:

            # тайм-аут может быть определен для ожидания отдельной строки (readTimeout)
            # и для ожидания нужной строки (matchTimeout). Могут также быть заданы два тайм-аута
            # одновременно или ни одного.
            #
            # Но все эти комбинации я так или иначе пересчитываю во время ожидания следующей строки

            if match_timeout is not None:
                match_elapsed = time.monotonic() - start_time
                match_left = match_timeout - match_elapsed

                if match_left <= 0:
                    # очень экзотический случай время закончилось не при ожидании строки,
                    # а между итерациями
                    raise LineWaitingTimeout

                if read_timeout is None:
                    curr_line_timeout = match_left
                else:
                    curr_line_timeout = min(read_timeout, match_left)

            else:
                assert match_timeout is None
                curr_line_timeout = read_timeout

            if curr_line_timeout is not None:
                try:
                    line = func_timeout(func=self.read_line, timeout=curr_line_timeout)
                except FunctionTimedOut:
                    raise LineWaitingTimeout
            else:
                line = self.read_line()

            if line is None:
                # процесс завершился, строка не найдена
                return False

            if predicate(line):
                # строка найдена
                return True

    @property
    def was_started(self):
        return self.thread is not None

    @property
    def is_running_thread(self):
        return self.thread is not None and self.thread.is_alive()

    @property
    def is_running_subprocess(self):
        return self._waited_subproc() is not None and self._waited_subproc().poll() is None

    def iter_lines(self) -> Iterator[str]:

        while True:

            line = self.read_line()
            if line is None:
                break
            yield line


class TestBackgroundProcess(unittest.TestCase):

    def testLineCapture(self):
        with BackgroundProcess(["echo", "lineA\nlineB\nlineC"]) as bp:
            lines = list(bp.iter_lines())
            self.assertEqual(lines, ['lineA', 'lineB', 'lineC'])

    def testWaitLine(self):
        # успешно находим
        with BackgroundProcess(["echo", "lineA\nlineB\nlineC"]) as bp:
            self.assertTrue(bp.wait_for_line(lambda s: s == "lineB"))

        # процесс завершается, но не находим
        with BackgroundProcess(["echo", "lineA\nlineB\nlineC"]) as bp:
            self.assertFalse(bp.wait_for_line(lambda s: s == "lineZ"))

        # ждем слишком долго - получаем исключение
        with self.assertRaises(LineWaitingTimeout):
            with BackgroundProcess(["sleep", "3"]) as bp:
                bp.wait_for_line(lambda s: s == "never!", read_timeout=0.25)

        # ждем слишком долго - получаем исключение
        with self.assertRaises(LineWaitingTimeout):
            with BackgroundProcess(["sleep", "3"]) as bp:
                bp.wait_for_line(lambda s: s == "never!", match_timeout=0.25)


if __name__ == "__main__":
    TestBackgroundProcess().testWaitLine()
