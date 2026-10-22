"""Talos: listens to Hermes and dispatches Projects4Me issues to headless Claude Code agents.

Run `dev-tools/talos/talos -h`; see dev-tools/talos/README.md.
"""

import argparse
import json
import ssl
import subprocess
import sys
import time
import urllib.request

from lib import db, policy, runner, sessions
from lib import runs as run_log
from lib.config import LISTENER, STATE_DIR, TALOS_DIR, TalosError, claude_bin, log, settings
from lib.dispatcher import Dispatcher
from lib.ideas import setup as ideas_setup
from lib.hermes import Listener
from lib.mcp import mcp_config
from lib.registry import load_agents
from lib.tracker import Tracker


def cmd_run(args):
    """Listen for every enabled agent until stopped."""
    cfg = settings()
    claude = claude_bin(cfg.get("claudeBin"))
    agents = load_agents(args.agent)
    imported = db.import_legacy()
    if imported:
        log(f"imported {imported} issue state(s) from sessions.json into talos.db")
    log(f"starting with {len(agents)} agent(s): {', '.join(a.name for a in agents)}; claude {claude}")
    for agent in agents:
        if agent.kind == "ideas":
            if args.dry_run:
                log("dry run: ideas agents are not started", agent.name)
                continue
            try:
                ideas_setup.start(agent, cfg, claude)
            except Exception as e:      # one agent's setup never keeps the others from running
                log(f"not started: {e}", agent.name)
            continue
        tracker = Tracker(agent)
        dispatcher = Dispatcher(agent, tracker, claude, dry_run=args.dry_run,
                                mcp=mcp_config(STATE_DIR / agent.name / "mcp.json"))
        dispatcher.recover_interrupted()
        dispatcher.start()
        Listener(agent, tracker, [policy.ASSIGNEE_EVENT], dispatcher.on_event, dispatcher.reconcile,
                 cfg["hermesUrl"], cfg.get("nodeBin", "node"), cfg.get("reconnectSeconds", 10)).start()
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        log("stopped")
    return 0


def cmd_dispatch(args):
    """Work one issue now, the same way an assignment event would (no Hermes needed)."""
    cfg = settings()
    agent = load_agents(args.agent)[0]
    db.import_legacy()
    tracker = Tracker(agent)
    dispatcher = Dispatcher(agent, tracker, claude_bin(cfg.get("claudeBin")), dry_run=args.dry_run,
                            mcp=mcp_config(STATE_DIR / agent.name / "mcp.json"))
    outcome = dispatcher.handle(args.issue)
    print(outcome or "skipped")
    return 0


def cmd_status(args):
    """What Talos is running now (with the live transcript's latest steps) and each issue's state."""
    entries = sessions.all_entries()
    running = {k: v for k, v in entries.items() if v.get("running")}
    if not running:
        print("Nothing running.")
    for (agent, issue), entry in sorted(running.items()):
        print(f"RUNNING  {agent} #{issue}  on {entry.get('last_model')}  iteration {entry.get('runs')}"
              f"  since {entry.get('started_at') or '?'}")
        path = runner.transcript(entry.get("live_session_id")) or runner.transcript_for_issue(issue)
        if not path:
            print("  transcript not found yet")
            continue
        print(f"  transcript {path}  (last write {time.strftime('%H:%M:%S', time.localtime(path.stat().st_mtime))})")
        for step in runner.recent_steps(path, args.steps):
            print(f"  {step}")
    others = sorted((k, v) for k, v in entries.items() if not v.get("running"))
    if others:
        print("\nIssues worked before:")
        for (agent, issue), entry in others:
            print(f"  {agent} #{issue}  runs {entry.get('runs') or 0}  last model {entry.get('last_model') or '-'}"
                  f"  last ended {entry.get('last_ended_at') or '-'}" + ("  escalated" if entry.get("escalated") else ""))
    return 0


def cmd_runs(args):
    """Per-run stats from talos.db: model, outcome, cost of that run alone, turns and context size."""
    rows = run_log.recent(args.limit, args.issue)
    if not rows:
        print("No runs recorded yet.")
        return 0
    print(f"{'id':>4} {'issue':>6} {'it':>2} {'model':7} {'resumed':7} {'started':16} {'min':>5} "
          f"{'outcome':12} {'cost':>6} {'turns':>5} {'peak':>5}")
    total = 0.0
    for r in rows:
        cost = r.get("cost_usd")
        total += cost or 0
        minutes = f"{r['duration_s'] / 60:.1f}" if r.get("duration_s") else "-"
        peak = f"{(r.get('peak_context_tokens') or 0) // 1000}K" if r.get("peak_context_tokens") else "-"
        print(f"{r['id']:>4} {r['issue']:>6} {r.get('iteration') or '-':>2} {r.get('model') or '-':7} "
              f"{'yes' if r.get('resumed') else 'no':7} {(r.get('started_at') or '')[:16]:16} {minutes:>5} "
              f"{r.get('outcome') or 'running':12} {('$%.2f' % cost) if cost is not None else '-':>6} "
              f"{r.get('turns') if r.get('turns') is not None else '-':>5} {peak:>5}")
    print(f"\n{len(rows)} run(s), ${total:.2f} in total")
    return 0


def cmd_reset(args):
    """Give an issue a fresh iteration allowance after a human has looked at it."""
    agent = load_agents(args.agent)[0]
    before = sessions.get(agent.name, args.issue)
    fields = {"runs": 0}
    if args.model:
        fields["escalated"] = False
    sessions.update(agent.name, args.issue, **fields)
    print(f"#{args.issue}: runs {before.get('runs', 0)} -> 0 (limit {agent.max_iterations})"
          + ("; escalation cleared" if args.model else ""))
    return 0


def cmd_agents(args):
    """List the registry, with each agent's Gaia identity and projects resolved."""
    for agent in load_agents(args.agent):
        line = {"name": agent.name, "role": agent.role, "enabled": agent.enabled,
                "models": {"default": agent.model_default, "byComplexity": agent.model_by_complexity,
                           "escalate": agent.model_escalate},
                "maxIterations": agent.max_iterations,
                "statuses": sorted(agent.actions), "permissionMode": agent.permission_mode}
        try:
            tracker = Tracker(agent)
            line["gaiaUser"] = tracker.user_id()
            line["projects"] = tracker.project_ids()
        except TalosError as e:
            line["error"] = str(e)
        print(json.dumps(line, indent=2))
    return 0


def cmd_check(args):
    """Preflight: Claude CLI, Node listener dependencies, Hermes health, each agent's sign-in."""
    cfg = settings()
    ok = True

    def report(name, fn):
        nonlocal ok
        try:
            print(f"ok    {name}: {fn()}")
        except Exception as e:
            ok = False
            print(f"FAIL  {name}: {e}")

    report("claude", lambda: claude_bin(cfg.get("claudeBin")))
    report("node", lambda: subprocess.run([cfg.get("nodeBin", "node"), "--use-system-ca", "-e",
                                           "require('socket.io-client');console.log(process.version)"],
                                          cwd=TALOS_DIR, capture_output=True, text=True, check=True).stdout.strip())
    report("listener", lambda: LISTENER.exists() or (_ for _ in ()).throw(TalosError("listen.js missing")))

    def hermes():
        with urllib.request.urlopen(cfg["hermesUrl"].rstrip("/") + "/health", timeout=5,
                                    context=ssl.create_default_context()) as r:
            return json.loads(r.read()).get("ok")
    report("hermes", hermes)
    agents = load_agents(args.agent)
    if any(a.kind == "ideas" and ideas_setup.source(a) == "sim" for a in agents):
        def sim():
            url = ideas_setup.sim_settings(cfg)["url"].rstrip("/")
            with urllib.request.urlopen(url + "/health", timeout=5) as r:
                return f"{url} ok={json.loads(r.read()).get('ok')}"
        report("sim (ideas)", sim)
    for agent in agents:
        report(f"agent {agent.name}", lambda a=agent: f"Gaia user {Tracker(a).user_id()}")
    return 0 if ok else 1


def build_parser():
    p = argparse.ArgumentParser(prog="talos", description="Dispatch Projects4Me issues to headless Claude Code agents.")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("run", help="listen to Hermes and dispatch assignments (until Ctrl+C)")
    sp.add_argument("--agent", help="only this agent")
    sp.add_argument("--dry-run", action="store_true", help="decide and log, but don't start Claude")
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("dispatch", help="work one issue now, as if it had just been assigned (issue agents)")
    sp.add_argument("agent")
    sp.add_argument("issue", type=int)
    sp.add_argument("--dry-run", action="store_true")
    sp.set_defaults(func=cmd_dispatch)

    sp = sub.add_parser("status", help="what is running now, with the live transcript's latest steps")
    sp.add_argument("--steps", type=int, default=10, help="how many recent steps to show (default 10)")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("runs", help="per-run stats: model, outcome, cost of the run alone, turns, context size")
    sp.add_argument("--issue", type=int, help="only this issue")
    sp.add_argument("--limit", type=int, default=20)
    sp.set_defaults(func=cmd_runs)

    sp = sub.add_parser("reset", help="reset an issue's iteration count after a manual look")
    sp.add_argument("agent")
    sp.add_argument("issue", type=int)
    sp.add_argument("--model", action="store_true", help="also drop a past escalation (back to complexity/default)")
    sp.set_defaults(func=cmd_reset)

    sp = sub.add_parser("agents", help="list registered agents")
    sp.add_argument("--agent")
    sp.set_defaults(func=cmd_agents)

    sp = sub.add_parser("check", help="preflight checks")
    sp.add_argument("--agent")
    sp.set_defaults(func=cmd_check)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except TalosError as e:
        print(f"talos {args.command}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
