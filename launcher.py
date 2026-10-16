"""
One entry point for the whole toolkit: `./mdp3 <command>`.

    ./mdp3 up                                 # simulator + ladder, ES
    ./mdp3 up --products ES,NQ,CL,ZN --capture     # + capture to S3 / ./pcaps
    ./mdp3 up --replay ./pcaps --speed 2      # replay a capture instead of simulating
    ./mdp3 up --no-ladder --consumer          # headless: consumer in the foreground
    ./mdp3 up --products ES,CL --dashboard    # + the browser dashboard, in live mode

    ./mdp3 sim | consumer | ladder | capture | replay | products | pcap | s3 | dashboard …
                                              # any one tool, arguments passed through
    ./mdp3 fetch-schema | fetch-config | fetch-cme
                                              # CME's templates_FixBinary.xml / config.xml into schema/
    ./mdp3 test                               # every test suite
    ./mdp3 deploy [--service]                 # push to the VM (deploy.sh)

`up` starts the background pieces (simulator or replay, capture) with their
output in logs/<name>.log and puts the ladder (or the consumer) in the
foreground. Everything stops — with each tool's summary — when you quit the
ladder (q) or press Ctrl-C; headless, also when the feed source finishes.
"""
from __future__ import annotations

import argparse
import os
import shlex
import signal
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
LOGS = os.path.join(HERE, "logs")
PY = sys.executable

TOOLS = {
    "sim": "simulator.py", "simulator": "simulator.py", "consumer": "consumer.py",
    "ladder": "ladder.py", "capture": "capture.py", "replay": "replay.py",
    "products": "products.py", "contracts": "contracts.py", "settlement": "settlement.py",
    "pcap": "pcap.py", "s3": "s3store.py", "dashboard": "app.py",
    "record": "record_session.py", "build-dashboard": "build_dashboard.py",
}
TESTS = ("test_roundtrip.py", "test_start_price.py", "test_products.py",
         "test_timestamps.py", "test_wire.py", "test_feeds.py", "test_pcap.py",
         "test_dashboard.py")


def tool(name: str) -> list[str]:
    return [PY, os.path.join(HERE, TOOLS[name])]


class Child:
    def __init__(self, name: str, argv: list[str]):
        self.name = name
        self.argv = argv
        os.makedirs(LOGS, exist_ok=True)
        self.log_path = os.path.join(LOGS, f"{name}.log")
        self.log = open(self.log_path, "w")
        self.proc = subprocess.Popen(argv, stdout=self.log, stderr=subprocess.STDOUT,
                                     stdin=subprocess.DEVNULL, cwd=os.getcwd(),
                                     start_new_session=True)   # our Ctrl-C isn't theirs

    def alive(self) -> bool:
        return self.proc.poll() is None

    def stop(self, grace: float = 5.0) -> None:
        if self.alive():
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(grace)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self.log.close()

    def tail(self, n: int = 12) -> str:
        try:
            with open(self.log_path) as fh:
                return "".join(fh.readlines()[-n:])
        except OSError:
            return ""


def up(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="mdp3 up", description="Run the feed end to end")
    ap.add_argument("--products", "--product", dest="products", default="ES",
                    help="up to 4 product codes, comma-separated (default ES)")
    ap.add_argument("--replay", metavar="PATH", default=None,
                    help="replay this capture (file, dir, s3:prefix) instead of simulating")
    ap.add_argument("--speed", type=float, default=1.0, help="replay timing multiplier")
    ap.add_argument("--duration", type=float, default=0,
                    help="simulate this long, seconds (default: until stopped)")
    ap.add_argument("--rate", type=float, default=20, help="book updates per second per product")
    ap.add_argument("--capture", action="store_true", help="also capture every feed to pcap")
    ap.add_argument("--local-dir", default=None,
                    help="capture to this directory (default ./pcaps when S3 isn't configured)")
    ap.add_argument("--interval", type=float, default=3.0, help="capture flush period")
    ap.add_argument("--no-ladder", action="store_true", help="don't open the ladder")
    ap.add_argument("--dashboard", action="store_true",
                    help="also serve the browser dashboard, opened on the live feed")
    ap.add_argument("--port", type=int, default=8000, help="dashboard port")
    ap.add_argument("--open", action="store_true", help="open the dashboard in a browser")
    ap.add_argument("--schema", default=None,
                    help="schema the simulator sends (cme|subset); receivers auto-detect")
    ap.add_argument("--consumer", action="store_true",
                    help="run the consumer in the foreground (implied by --no-ladder)")
    ap.add_argument("--iface", default="127.0.0.1",
                    help="interface for every tool; receivers take a comma-separated list, "
                         "the simulator/replay send on the first")
    ap.add_argument("--ttl", type=int, default=0, help="simulator / replay multicast TTL")
    ap.add_argument("--loss-a", type=float, default=0.0, help="drop this fraction of feed A")
    ap.add_argument("--loss-b", type=float, default=0.0, help="drop this fraction of feed B")
    ap.add_argument("--sim-args", default="", help="extra simulator flags, quoted")
    ap.add_argument("--offline", action="store_true", help="simulator never fetches closes")
    args = ap.parse_args(argv)

    products = args.products
    sys.path.insert(0, HERE)
    from receiver import first_iface, parse_ifaces
    try:
        args.iface = ",".join(parse_ifaces(args.iface))
    except ValueError as e:
        ap.error(str(e))
    send_iface = first_iface(args.iface)
    children: list[Child] = []
    foreground = None
    try:
        if args.capture:
            import s3store
            s3store.load_env_file()
            cap = tool("capture") + ["--product", products, "--iface", args.iface,
                                     "--interval", str(args.interval)]
            local = args.local_dir or (None if s3store.configured() else "pcaps")
            if local:
                cap += ["--local-dir", local]
            children.append(Child("capture", cap))
            time.sleep(0.5)                       # joined before the first packet

        if args.replay:
            src = tool("replay") + [args.replay, "--speed", str(args.speed),
                                    "--product", products, "--iface", send_iface,
                                    "--ttl", str(args.ttl)]
            name = "replay"
        else:
            src = tool("sim") + ["--product", products, "--rate", str(args.rate),
                                 "--duration", str(args.duration or 10 ** 9),
                                 "--iface", send_iface, "--ttl", str(args.ttl),
                                 "--loss-a", str(args.loss_a), "--loss-b", str(args.loss_b),
                                 "--quiet"]
            if args.offline:
                src.append("--offline")
            if args.schema:
                src += ["--schema", args.schema]
            src += shlex.split(args.sim_args)
            name = "simulator"

        if args.dashboard:
            dash = Child("dashboard", tool("dashboard") + ["--port", str(args.port)])
            children.append(dash)
            url = (f"http://127.0.0.1:{args.port}/?mode=live&products={products}"
                   f"&iface={args.iface}")
            print(f"  dashboard {url}", flush=True)
            if args.open:
                import webbrowser
                time.sleep(1.0)
                webbrowser.open(url)

        use_ladder = not args.no_ladder and not args.consumer and sys.stdout.isatty()
        fg_argv = (tool("ladder") if use_ladder else tool("consumer") + ["--summary"]) \
            + ["--product", products, "--iface", args.iface]

        print(f"mdp3 up: {products}  ({'replay ' + args.replay if args.replay else 'simulator'})"
              f"{'  + capture' if args.capture else ''}  logs in {os.path.relpath(LOGS)}/", flush=True)
        if use_ladder:
            # the ladder joins first, so it sees the startup definitions + snapshot
            foreground = subprocess.Popen(fg_argv)
            time.sleep(0.4)
            source = Child(name, src)
            children.append(source)
            foreground.wait()                    # the ladder stays up until you press q
        else:
            foreground = subprocess.Popen(fg_argv, start_new_session=True)
            time.sleep(0.4)
            source = Child(name, src)
            children.append(source)
            for c in children:
                print(f"  {c.name:<9} pid {c.proc.pid}  log {os.path.relpath(c.log_path)}", flush=True)
            while foreground.poll() is None and source.alive():
                time.sleep(0.2)
            time.sleep(0.5)                       # let the last packets land
    except KeyboardInterrupt:
        pass
    finally:
        if foreground is not None and foreground.poll() is None:
            foreground.send_signal(signal.SIGTERM)
            try:
                foreground.wait(5)
            except subprocess.TimeoutExpired:
                foreground.kill()
        for c in reversed(children):
            c.stop()
        for c in children:
            print(f"\n── {c.name} ({os.path.relpath(c.log_path)}) " + "─" * 30)
            print(c.tail().rstrip())
    return 0


def run_tests() -> int:
    failed = []
    for t in TESTS:
        r = subprocess.run([PY, os.path.join(HERE, t)], cwd=HERE,
                           capture_output=True, text=True)
        last = (r.stdout.strip().splitlines() or [""])[-1]
        print(f"{'PASS' if r.returncode == 0 else 'FAIL'}  {t:<22} {last}")
        if r.returncode:
            failed.append(t)
            print(r.stdout[-2000:] + r.stderr[-2000:])
    return 1 if failed else 0


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help", "help"):
        print(__doc__.strip())
        return 0
    cmd, rest = sys.argv[1], sys.argv[2:]
    if cmd == "up":
        return up(rest)
    if cmd == "test":
        return run_tests()
    if cmd in ("fetch-schema", "fetch-config", "fetch-cme"):
        flag = {"fetch-schema": ["--schema"], "fetch-config": ["--config"], "fetch-cme": []}[cmd]
        os.execv(PY, [PY, os.path.join(HERE, "cme_files.py"), *flag, *rest])
    if cmd == "deploy":
        os.execv("/bin/bash", ["bash", os.path.join(HERE, "deploy.sh"), *rest])
    if cmd in TOOLS:
        os.execv(PY, tool(cmd) + rest)
    print(f"mdp3: unknown command {cmd!r}\n\n{__doc__.strip()}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
