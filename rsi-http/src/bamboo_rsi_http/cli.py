"""HTTP enterprise integrations and asynchronous RSI control API."""
import argparse
import json
from pathlib import Path

from bamboo_rsi.codex import CodexProposer
from bamboo_rsi.storage import identity, read, write
from .gateway import Gateway, create_server
from .remote import integration, parse_manifest
from .transport import JsonClient


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    inspect = commands.add_parser("inspect", help="Read a service manifest without executing a task")
    inspect.add_argument("--endpoint-config", required=True)
    inspect.add_argument("--output", required=True)
    check = commands.add_parser("check")
    check.add_argument("--config", required=True)
    demo = commands.add_parser("demo-http", help="Loopback fixtures; no real model calls or production acceptance")
    demo.add_argument("--destination", required=True)
    serve = commands.add_parser("serve")
    serve.add_argument("--catalog", required=True)
    serve.add_argument("--home", required=True)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8079)
    serve.add_argument("--token-env", default="RSI_CONTROL_TOKEN")
    args = parser.parse_args(argv)
    if args.command == "inspect":
        manifest = JsonClient(read(args.endpoint_config)).request("GET", "/rsi/v1/manifest")
        spec, _ = parse_manifest(manifest)
        write(args.output, manifest, immutable=True)
        result = {"task_id": spec.id, "manifest_sha256": identity(manifest), "execution_started": False}
    elif args.command == "check":
        plugin = integration(read(args.config))
        result = {"task": plugin.adapter.describe().value(), "fingerprint": plugin.fingerprint(), "execution_started": False}
    elif args.command == "demo-http":
        from .demo import run_demo
        result = run_demo(args.destination)
    else:
        catalog = read(args.catalog)
        if set(catalog) - {"integrations", "optimizer"}:
            raise ValueError("Unknown catalog option")
        plugins = {}
        for config in catalog["integrations"]:
            plugin = integration(config)
            identifier = plugin.adapter.describe().id
            if identifier in plugins:
                raise ValueError("Duplicate integration identity")
            plugins[identifier] = plugin
        optimizer = catalog.get("optimizer", {})
        if set(optimizer) - {"executable", "model"}:
            raise ValueError("Unknown optimizer option")
        def proposer(flow):
            return CodexProposer(optimizer.get("executable", "codex"), model=optimizer.get("model"), stop_path=flow.root / "STOP")
        gateway = Gateway(args.home, plugins, proposer_factory=proposer)
        server = None
        try:
            server = create_server((args.host, args.port), gateway, args.token_env)
            print(json.dumps({"listening": f"http://{args.host}:{server.server_port}", "authenticated": True}), flush=True)
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            if server: server.server_close()
            gateway.close()
        return 0
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
