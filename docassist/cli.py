from __future__ import annotations

import json
import sys

import typer

app = typer.Typer(add_completion=False, help="docassist: permission-aware RAG, eval gate and guarded agent")


def _print(obj) -> None:
    typer.echo(json.dumps(obj, indent=2, default=str))


@app.command()
def init() -> None:
    """Create / migrate the database schema."""
    from docassist.db.conn import init_schema

    init_schema()
    typer.echo("schema ready")


@app.command()
def generate(seed: int = 42) -> None:
    """Generate the synthetic corpus, directory, business data and eval questions."""
    from docassist.data.generate import generate as gen

    _print(gen(seed))


@app.command()
def index(fault_after: int = typer.Option(None, help="inject a crash after N items (recovery demo)")) -> None:
    """Incremental indexing run (new / changed / acl-only / deleted files only)."""
    from docassist.db.conn import init_schema
    from docassist.ingest.indexer import index_stats, load_business, run_index

    init_schema()
    load_business()
    last = {"processed": -1}

    def progress(st: dict) -> None:
        if st["processed"] - last["processed"] >= 256 or st.get("status"):
            last["processed"] = st["processed"]
            typer.echo(f"  {st['processed']}/{st['total']} processed, embedded={st['embedded_chunks']}", err=True)

    try:
        _print(run_index(fault_after=fault_after, progress=progress))
    finally:
        _print(index_stats())


@app.command()
def simulate(new: int = 5, edit: int = 5, delete: int = 2, acl: int = 3, seed: int = None) -> None:
    """Mutate the corpus to demonstrate incremental indexing."""
    from docassist.data.changes import simulate_changes

    _print(simulate_changes(new, edit, delete, acl, seed))


@app.command()
def ask(question: str, user: str = typer.Option(..., "--user", "-u"), answerer: str = None) -> None:
    """Ask a question as a user."""
    from docassist.answer.rag import answer_question

    a = answer_question(user, question, answerer=answerer)
    typer.echo(f"\n{a.answer}\n")
    for c in a.citations:
        typer.echo(f"  [{c['n']}] {c['doc_id']}  ({c['title']})")
    typer.echo(f"\nretrieval: {json.dumps({k: v for k, v in a.debug.items() if k != 'principals'}, default=str)}",
               err=True)


@app.command()
def agent(message: str, user: str = typer.Option(..., "--user", "-u")) -> None:
    """Talk to the guarded agent as a user."""
    from docassist.agent.loop import run_agent

    r = run_agent(user, message)
    for st in r.steps:
        typer.echo(f"  tool={st['tool']} decision={st['decision']} reason={st.get('reason')} rows={st.get('row_count')}")
    typer.echo(f"\n{r.reply}")


@app.command(name="eval")
def eval_cmd(
    gate: bool = typer.Option(False, help="exit 1 if the release gate fails"),
    label: str = "",
    resume: str = typer.Option(None, help="resume an interrupted run id"),
    limit: int = None,
    chunk_size: int = None,
    chunk_overlap: int = None,
    top_k: int = None,
    hybrid: bool = typer.Option(None, "--hybrid/--no-hybrid"),
    answerer: str = None,
    update_baseline: bool = typer.Option(False, help="store this run's metrics as the accepted baseline"),
    summary_md: str = typer.Option(None, help="write a markdown summary (e.g. $GITHUB_STEP_SUMMARY)"),
) -> None:
    """Run the evaluation set; optionally enforce the release gate."""
    from docassist.evaluation.experiments import apply_config

    apply_config(chunk_size, chunk_overlap, top_k, hybrid, answerer)
    from docassist.config import get_settings
    from docassist.db.conn import init_schema
    from docassist.evaluation.runner import run_eval
    from docassist.ingest.indexer import index_stats, run_index

    init_schema()
    typer.echo("incremental index for this configuration ...", err=True)
    typer.echo(json.dumps({k: run_index()[k] for k in ("plan", "embedded_chunks")}), err=True)
    typer.echo(json.dumps(index_stats()), err=True)
    rep = run_eval(label=label, resume=resume, limit=limit,
                   progress=lambda p: typer.echo(f"  {p['done']}/{p['total']}", err=True) if p["done"] % 10 == 0 else None)
    md = render_summary(rep)
    typer.echo(md)
    if summary_md:
        with open(summary_md, "a") as f:
            f.write(md + "\n")
    if update_baseline:
        keep = {k: v for k, v in rep["metrics"].items() if not isinstance(v, dict)}
        (get_settings().eval_dir / "baseline.json").write_text(json.dumps(keep, indent=1) + "\n")
        typer.echo("baseline updated", err=True)
    if gate and not rep["gate"]["passed"]:
        typer.echo(f"RELEASE GATE FAILED: {', '.join(rep['gate']['failed'])}", err=True)
        sys.exit(1)


def render_summary(rep: dict) -> str:
    g = rep["gate"]
    lines = [f"## Eval run `{rep['run_id']}` - gate {'PASSED' if g['passed'] else 'FAILED'}", "",
             f"config: `{json.dumps(rep['config'])}`", "",
             "| check | kind | value | threshold | baseline | result |", "|---|---|---|---|---|---|"]
    for c in g["checks"]:
        lines.append(f"| {c['metric']} | {c['kind']} | {c['value']} | {c['threshold']} | {c.get('baseline', '')} | "
                     f"{'pass' if c['passed'] else '**FAIL**'} |")
    failed = [r for r in rep["results"] if not r["passed"]]
    lines += ["", f"{len(rep['results']) - len(failed)}/{len(rep['results'])} questions passed.", ""]
    if failed:
        lines += ["| qid | type | user | reasons |", "|---|---|---|---|"]
        lines += [f"| {r['qid']} | {r['type']} | {r['user']} | {'; '.join(r['fail_reasons'])} |" for r in failed[:30]]
    return "\n".join(lines)


@app.command()
def grant(user: str, group: str, revoke: bool = False) -> None:
    """Change a group membership (directory change) - effective immediately, no re-index."""
    from docassist.data.changes import set_group_membership

    _print(set_group_membership(user, group, not revoke))


if __name__ == "__main__":
    app()
