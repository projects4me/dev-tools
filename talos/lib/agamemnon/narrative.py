"""The issue comment Agamemnon posts for each action it takes: the issue's episodic memory, the record people read
(the journal is for undo). Pure: it builds the title and HTML body from what the caller passes in."""

import html
import json

NEXT = {
    "backup": "nothing is waiting on this; the workspace is saved for a later restore",
    "restore": "the worker's next run starts from the restored files",
    "retry": "waiting on {worker}, who runs the issue again",
    "rerun-ci": "waiting on the CI re-run; the merge gate is checked again when it finishes",
    "wait-for": "{wait}",
    "hold-until-tomorrow": "held by {me} until {until}, then given back to {worker}",
    "hold-until-docker": "held by {me} until Docker is ok, then given back to {worker}; the owner gets it if Docker "
                         "is not back by {until}",
    "hold-until-done": "held by {me} until the issue it waits for is done, then the base is merged into its PR and "
                       "CI runs again; the owner gets it if that is not done by {until}",
    "start-docker": "{docker}",
    "restart-docker": "{docker}",
    "stop-stale-containers": "the freed memory is available to the runs waiting for Docker; the next `ai test` of a "
                             "stopped issue starts its containers again",
    "send": "waiting on {worker}, who has the notes above",
    "update-branch": "waiting on CI to run on the updated branch",
    "push-merge": "waiting on CI to run on the pushed merge",
    "merge": "the PR is merged; nothing is waiting",
    "file-bug": "{bug} tracks the failure, which is on the base branch too; once it is fixed the base is merged "
                "into this PR and CI runs again",
}


def _next(verb, result, settings):
    worker, me = settings.worker.display_name, settings.display_name
    if verb == "wait-for":
        wait = (f"the wait for {result.get('waitedFor')} is over: it is ready, so the next step can go ahead"
                if result.get("ready") else
                f"waited {result.get('seconds')}s for {result.get('waitedFor')} and it is still not ready; "
                f"Agamemnon retries later or sends the issue to the owner")
        return NEXT[verb].format(wait=wait)
    docker = ("Docker answers again" + (f"; stack containers not running: {', '.join(result['stackMissing'])}"
                                         if result.get("stackMissing") else "")
              if result.get("ok") else f"Docker did not come back ({result.get('error') or 'no reason given'}); "
                                       f"Agamemnon retries or sends the issue to the owner")
    bug = ((f"Bug #{result.get('bug')}" + (f" ({result['url']})" if result.get("url") else "")
            + (", filed earlier for the same signature," if result.get("linked") else f", filed for {worker},"))
           if verb == "file-bug" else "")
    return NEXT[verb].format(worker=worker, me=me, until=result.get("heldUntil"), docker=docker, bug=bug)


def _what(verb, args):
    shown = {k: v for k, v in (args or {}).items() if v not in (None, "", [])}
    return f"ran <code>{html.escape(verb)}</code>" + (f" with <code>{html.escape(json.dumps(shown))}</code>"
                                                      if shown else "")


def title(settings, verb):
    return f"{settings.display_name}: {verb}"


def body(settings, incident, verb, args, why, result, action_id, undo_note):
    """HTML for the comment: what it saw, what it did, how to undo it, what happens next."""
    e = html.escape
    undo = (f"journal action {action_id}: <code>talos agamemnon undo {action_id}</code>. {e(undo_note)}"
            if undo_note and "cannot be undone" not in undo_note else
            f"journal action {action_id}. {e(undo_note or 'This cannot be undone.')}")
    undo = undo.replace("&lt;id&gt;", str(action_id))
    parts = (("What I saw", f"incident #{incident['id']} of type <code>{e(incident['type'])}</code>: {e(str(why))}"),
             ("What I did", _what(verb, args)),
             ("How to undo", undo),
             ("What happens next", e(_next(verb, result, settings))))
    return "".join(f"<p><b>{label}:</b> {text}</p>" for label, text in parts)
