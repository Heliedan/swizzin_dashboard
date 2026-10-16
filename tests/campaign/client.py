#!/usr/bin/python3
"""One App Store installation in a throwaway Swizzin, driven the way the page drives it (see run.py).

Run as root inside the container, with the system Python (standard library only):
  python3 -I client.py <app> <transcript file> [answers]   install <app>, print one JSON line with the outcome
  python3 -I client.py --catalogue                          print the helper's catalogue

The installation goes through the helper's socket, like the panel. Questions are answered with their
default (Enter), or with a throwaway password when a password is asked, or with the answer given for
them in `answers` (JSON: [[regular expression, answer], ...], see ANSWERS in run.py), and every question
met is reported. A question is detected when a process of the installation waits on its terminal (read on
the terminal, or whiptail waiting for a key) and the output has been quiet for a moment.
"""
import json
import os
import re
import select
import socket
import sys
import time

SOCKET = "/run/swizzin-store/store.sock"
PASSWORD = "Campaign-Test-2026"  # typed when an installer asks for a password: throwaway machines only
QUIET = 2.0  # seconds without output before answering a question
TIMEOUT = 100 * 60  # the whole installation; Ctrl+C after that
MAX_ANSWERS = 40
SAME_QUESTION = 4  # the same question this many times in a row: the installer is going round in circles
READS = {0, 17, 19, 295}  # read, pread64, readv, preadv (x86_64 system call numbers)
POLLS = {7, 23, 270, 271}  # poll, select, pselect6, ppoll
INTERACTIVE = {"whiptail", "dialog"}  # programs that wait for a key in poll/select


def plain(text):
    """Terminal output as plain words: whiptail draws with cursor moves and line-drawing characters."""
    text = re.sub(r"\x0e[^\x0f]*\x0f", " ", text)  # DEC line drawing
    text = re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]", " ", text)  # cursor moves, colours
    text = re.sub(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)", "", text)  # window title
    text = re.sub(r"\x1b[()][0-9A-Za-z]|\x1b[=>78DEHMNOc]", "", text)
    text = re.sub(r"[─-▟]", " ", text)  # box drawing
    text = re.sub(r"[^\S\n]+", " ", text.replace("\r", "\n"))
    return re.sub(r"\n(?: ?\n)+", "\n", text)


def terminal_reader():
    """The name of an installation process waiting for an answer on its terminal, or None."""
    for pid in filter(str.isdigit, os.listdir("/proc")):
        try:
            with open(f"/proc/{pid}/syscall") as f:
                fields = f.read().split()
            if len(fields) < 2 or not fields[0].isdigit():
                continue  # running, or not in a system call
            with open(f"/proc/{pid}/comm") as f:
                comm = f.read().strip()
            number = int(fields[0])
            if number in READS:
                target = os.readlink(f"/proc/{pid}/fd/{int(fields[1], 16)}")
            elif number in POLLS and comm in INTERACTIVE:
                target = os.readlink(f"/proc/{pid}/fd/0")
            else:
                continue
            if target.startswith("/dev/pts/") or target == "/dev/tty":
                return comm
        except (OSError, ValueError):
            continue
    return None


def reply(question, rules):
    """What to type: the scripted answer whose pattern matches the end of the question, the throwaway
    password when a password is asked, else Enter (the installer's default)."""
    end = question[-200:]
    for pattern, text in rules:
        if pattern.search(end):
            return text + "\r", "scripted: " + text
    if re.search(r"pass(?:word|phrase)", question[-160:], re.I):
        return PASSWORD + "\r", "password"
    return "\r", "Enter"


class Helper:
    def __init__(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(SOCKET)
        self.buf = b""

    def send(self, obj):
        self.sock.sendall(json.dumps(obj).encode() + b"\n")

    def messages(self, timeout):
        """The messages received within `timeout` seconds; None when the helper closed the connection."""
        if not select.select([self.sock], [], [], timeout)[0]:
            return []
        chunk = self.sock.recv(65536)
        if not chunk:
            return None
        self.buf += chunk
        lines = self.buf.split(b"\n")
        self.buf = lines.pop()
        return [json.loads(line) for line in lines]


def install(app, transcript, answers=()):
    rules = [(re.compile(pattern, re.I), text) for pattern, text in answers]
    out = {"app": app, "prompts": [], "errors": []}
    helper = Helper()
    helper.send({"op": "install", "app": app, "rows": 40, "cols": 120})
    start = last_output = time.monotonic()
    test_start = interrupted = None
    screen = tail = ""  # output since the last answer / the last output, for the report
    done = None
    with open(transcript, "w", encoding="utf-8") as log:
        while done is None:
            messages = helper.messages(0.5)
            if messages is None:
                out["errors"].append("helper-closed")
                break
            for msg in messages:
                if "output" in msg:
                    log.write(msg["output"])
                    log.flush()
                    screen, tail = (screen + msg["output"])[-20000:], (tail + msg["output"])[-20000:]
                    last_output = time.monotonic()
                elif "phase" in msg:
                    test_start = time.monotonic()
                    out["install_seconds"] = round(test_start - start)
                elif "done" in msg:
                    done = msg["done"]
                elif "started" in msg:
                    out["job"] = msg["started"]["id"]
                elif msg.get("ok") is False:
                    out["errors"].append(msg.get("error"))
                    if "job" not in out:  # refused: nothing started
                        done = {}
            now = time.monotonic()
            if done is not None:
                break
            if interrupted is not None:
                if now - interrupted > 120:
                    out["errors"].append("ctrl-c-ignored")
                    break
                continue
            reader = None
            if now - start > TIMEOUT:
                out["errors"].append("timeout")
            elif test_start is None and now - last_output >= QUIET and (reader := terminal_reader()):
                question = plain(screen).strip()[-300:]
                recent = [p["question"] for p in out["prompts"][-(SAME_QUESTION - 1):]]
                if len(out["prompts"]) >= MAX_ANSWERS:
                    out["errors"].append("too-many-questions")
                elif len(recent) == SAME_QUESTION - 1 and all(q == question for q in recent):
                    out["errors"].append("same-question-again")
                else:
                    data, answer = reply(question, rules)
                    helper.send({"op": "input", "data": data})
                    out["prompts"].append({"after": round(now - start), "program": reader,
                                           "question": question, "answer": answer})
                    screen, last_output = "", now
                    continue
            else:
                continue
            helper.send({"op": "input", "data": "\x03"})  # Ctrl+C, like the page's button
            interrupted = now
    end = time.monotonic()
    done = done or {}
    out.update(result=done.get("result"), test=done.get("test"), generic_test=done.get("generic_test"),
               seconds=round(end - start), tail=plain(tail).strip()[-2000:])
    if test_start is not None:
        out["test_seconds"] = round(end - test_start)
    return out


def main():
    if sys.argv[1:] == ["--catalogue"]:
        helper = Helper()
        helper.send({"op": "catalogue"})
        messages = []
        while not messages:
            messages = helper.messages(10)
            if messages is None:
                sys.exit("the helper closed the connection")
        print(json.dumps(messages[0]))
        return
    app, transcript = sys.argv[1:3]
    answers = json.loads(sys.argv[3]) if len(sys.argv) > 3 else []
    print(json.dumps(install(app, transcript, answers)))


if __name__ == "__main__":
    main()
