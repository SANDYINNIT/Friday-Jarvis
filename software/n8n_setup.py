"""Private optional n8n setup and explicit terminal console. No keys in argv."""
import argparse
import getpass
import json
import sys
import warnings
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
from source.server.n8n_bridge import Bridge, CAPABILITIES, Config, Refused, public_operation


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, "Invalid setup arguments. Never pass credentials or private endpoints in command arguments.\n")


def hidden(prompt):
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        raise Refused("interactive_private_prompt_required")
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            return getpass.getpass(prompt)
        except (getpass.GetPassWarning, OSError, EOFError):
            raise Refused("private_prompt_unavailable") from None


def wizard(bridge):
    prior=bridge.config.read()
    print("Optional n8n bridge. Off by default; no account/credentials are bundled.")
    print("Saving does not activate the n8n workflow or make a connection.")
    changes={}
    deployment=hidden("Deployment origin (hidden, blank keeps saved): ").strip()
    if deployment:changes["deployment"]=deployment
    secret=hidden("Shared credential (hidden, blank keeps saved): ").strip()
    if secret:changes["secret"]=secret
    print("Available capabilities: "+", ".join(CAPABILITIES))
    choices=input("Explicit allowed capabilities, comma-separated (empty = none): ").strip()
    changes["capabilities"]=[c.strip() for c in choices.split(",") if c.strip()]
    changes["enabled"]=input("Enable selected requests? [y/N]: ").strip().lower()=="y"
    changes["notifications"]=input("Allow event pulls while this host runs? [y/N]: ").strip().lower()=="y"
    result=bridge.save(changes,prior["revision"])
    print(json.dumps(result,indent=2));return 0 if result["ok"] else 2


def console(bridge):
    if not sys.stdin.isatty():raise Refused("interactive_console_required")
    print("Explicit n8n console. Commands: status, pull, inbox, review EVENT_ID, approve EVENT_ID, reject EVENT_ID, quit.")
    print("No automatic polling or model tools. Review and approval share this process/session. Exit makes old decisions stale.")
    previews={}
    while True:
        command=input("n8n> ").strip().split()
        if not command:continue
        if command==["quit"]:return 0
        result={"ok":False,"error":"unsupported_command"}
        try:
            if command==["status"]:result=public_operation(bridge.operate("status",{}))
            elif command==["pull"]:result=public_operation(bridge.pull())
            elif command==["inbox"]:result=bridge.inbox()
            elif len(command)==2 and command[0]=="review":
                from source.server.redaction import redact_secret_values
                preview=bridge.decision_preview(command[1])
                previews[command[1]]=preview["digest"]
                result={"ok":True,**preview,"arguments":preview["arguments"]}
            elif len(command)==2 and command[0] in ("approve","reject"):
                digest=previews.pop(command[1],None)
                if digest and input("Confirm "+command[0]+" for reviewed exact event? Type CONFIRM: ")=="CONFIRM":
                    result=public_operation(bridge.decide(command[1],digest,command[0]))
                else:result={"ok":False,"error":"exact_review_required"}
        except Refused as error:result={"ok":False,"error":str(error)}
        print(json.dumps(result,indent=2,ensure_ascii=False))


def main(argv=None):
    parser=SafeParser(description="Private optional n8n setup. No browser/public ingress or ordinary-chat redirect.")
    parser.add_argument("command",choices=["show","wizard","test","disable","remove-key","console"])
    parser.add_argument("--yes-connect",action="store_true")
    args=parser.parse_args(argv)
    bridge=Bridge()
    try:
        if args.command=="show":result=bridge.snapshot()
        elif args.command=="wizard":return wizard(bridge)
        elif args.command=="console":return console(bridge)
        elif args.command=="test":
            if not args.yes_connect:raise Refused("explicit_connection_test_required")
            result=public_operation(bridge.operate("status",{}))
        else:
            current=bridge.config.read()
            if input("Type CONFIRM to "+args.command+" this n8n integration: ")!="CONFIRM":
                raise Refused("cancelled")
            result=bridge.save({"enabled":False} if args.command=="disable" else {"remove_secret":True},current["revision"])
        print(json.dumps(result,indent=2));return 0 if result.get("ok") else 2
    except (OSError,ValueError,EOFError,KeyboardInterrupt) as error:
        print(str(error) if isinstance(error,Refused) else "Operation unavailable; private inputs were not logged.",file=sys.stderr);return 2

if __name__=="__main__":raise SystemExit(main())
