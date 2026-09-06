"""Static setup evidence; never dispatches a worker or probes a live provider."""
from __future__ import annotations

from typing import Optional

from puppetmaster import diagnostics, platform_lock
from puppetmaster.model_registry import load_registry
from puppetmaster.platform_billing import detect_adapter_billing
from puppetmaster.preflight import _cached_catalog_membership, preflight_check
from puppetmaster.providers import available_providers, get_provider


def check(status: str, evidence: str, remedy: str = "") -> dict:
    return {"status": status, "evidence": [evidence], "remedy": remedy}


def installation_check(status: Optional[str]) -> dict:
    if status in {"installed", "unchanged"}:
        return check("pass", f"installer:{status}")
    if status == "would_install":
        return check("skipped", "installer:dry_run", "Re-run setup without --dry-run.")
    if status is None:
        return check("unknown", "No installation result recorded.", "Re-run setup for this platform.")
    return check("fail", f"installer:{status}", "Fix the installation error above and re-run setup.")


def _model_check(spec) -> dict:
    remedy = "Run `puppetmaster models discover --source " + (
        "anthropic" if spec.adapter == "claude-code" else spec.adapter
    ) + " --write`, then inspect `puppetmaster models list`."
    if not spec.enabled or spec.retired:
        return check("fail", f"Registry model {spec.id} is disabled or retired.",
                     "Select an enabled, non-retired registry model.")
    cached = _cached_catalog_membership(spec.adapter, spec.adapter_model_name)
    if cached is None:
        return check("unknown", "No usable discovery snapshot for this model.", remedy)
    ok, reason, evidence = cached
    status = "fail" if not ok else (
        "pass" if "preflight:cached_catalog_match" in evidence else "unknown"
    )
    return check(status, reason, "" if status == "pass" else remedy)


def collect_setup_readiness(
    enabled_adapters: set[str],
    *,
    installation_results: Optional[dict[str, str]] = None,
    installation_rc: int = 0,
    host_pilots: Optional[set[str]] = None,
) -> dict:
    """Return JSON-safe evidence for each registry model, including disabled rows.

    A static pass confirms local prerequisites only. Actual execution is always
    skipped here; the user must explicitly run a job and inspect its artifacts.
    Installation return codes are evidence, not worker-readiness verdicts.
    """
    installations = installation_results or {}
    rows = []
    try:
        specs = load_registry() if enabled_adapters else []
        registry_error = None
    except (OSError, RuntimeError, ValueError) as exc:
        specs = []
        registry_error = type(exc).__name__
    for adapter in sorted(enabled_adapters):
        known = adapter in platform_lock.KNOWN_ADAPTERS
        installed = installation_check(installations.get(adapter))
        if adapter in {"agentic", "openai"}:
            installed = check("pass", "Built-in adapter; no host installation required.")
        cli_present = {
            "codex": diagnostics._codex_cli_installed,
            "claude-code": diagnostics._claude_code_installed,
            "hermes": diagnostics._hermes_cli_installed,
            "antigravity": diagnostics._antigravity_installed,
        }.get(adapter)
        if cli_present and not cli_present():
            installed = check("fail", f"{adapter} CLI missing.",
                              f"Install the {adapter} CLI and re-run setup.")
        billing = detect_adapter_billing(adapter) if known else None
        credentials = check(
            "pass" if billing and billing.healthy else "fail",
            billing.detail if billing else f"Unknown adapter: {adapter}",
            "" if billing and billing.healthy else f"Configure credentials for {adapter}; run `puppetmaster doctor`.",
        )
        if not known:
            installed = check("fail", f"Unknown adapter: {adapter}", "Select a supported worker platform.")
        adapter_specs = [s for s in specs if s.adapter == adapter]
        for spec in adapter_specs or [None]:
            model = _model_check(spec) if spec else check(
                "unknown" if registry_error else "fail",
                f"Registry unreadable ({registry_error})." if registry_error else "No registry model for this adapter.",
                "Run `puppetmaster models init` or discover models for this adapter.",
            )
            model_credentials = credentials
            if adapter == "agentic" and spec:
                provider = spec.payload_defaults.get("provider")
                if not provider or get_provider(provider) is None:
                    model_credentials = check("unknown", "Model has no known explicit provider.",
                                              "Set payload_defaults.provider to a supported provider in the registry.")
                elif provider not in available_providers():
                    model_credentials = check("fail", f"Provider {provider} is not available.",
                                              f"Configure and enable credentials for {provider}.")
            if known and not platform_lock.is_adapter_enabled(adapter):
                preflight = check("fail", "Worker platform disabled by platform lock.",
                                  f"Run `puppetmaster platform enable {adapter}` if intended.")
            elif not known or installed["status"] == "fail" or model_credentials["status"] != "pass":
                preflight = check("fail", "Installation or credentials prerequisite not satisfied.",
                                  "Resolve installation and credential checks first.")
            elif installed["status"] != "pass":
                preflight = check("unknown", "Installation has not been confirmed.", installed["remedy"])
            elif spec is None or model["status"] == "fail":
                preflight = check("fail", "No available registry model.", model["remedy"])
            else:
                result = preflight_check(adapter, spec.adapter_model_name, live=False, billing_status=billing)
                preflight = check("pass" if result.ok else "fail", result.reason,
                                  "" if result.ok else "Run `puppetmaster preflight --help` and resolve the reported failure.")
            rows.append({
                "adapter": adapter, "model": spec.id if spec else None,
                "installed": installed, "credentials_ready": model_credentials,
                "model_available": model, "preflight_passed": preflight,
                "first_run_verified": check("skipped", "Setup did not execute a worker or call a live provider.",
                                            "Explicitly run a job on this model and inspect its delivered artifacts; provider charges may apply."),
            })
    for host in sorted(host_pilots or set()):
        rows.append({
            "adapter": host, "model": None, "host_only": True,
            "installed": installation_check(installations.get(host)),
            **{name: check("skipped", "Host pilot only; no worker selected.",
                           "Enable a worker platform separately to run jobs.")
               for name in ("credentials_ready", "model_available", "preflight_passed", "first_run_verified")},
        })
    install_states = {row["installed"]["status"] for row in rows}
    installed_status = (
        "fail" if installation_rc or "fail" in install_states else
        "pass" if install_states == {"pass"} else
        "skipped" if install_states == {"skipped"} else "unknown"
    )
    return {
        "schema_version": 1,
        "installation_rc": installation_rc,
        "installed": check(installed_status,
                           f"Setup installation return code: {installation_rc}; see per-platform results.",
                           "" if installation_rc == 0 else "Fix installation errors and re-run setup."),
        "targets": rows,
        "first_run_verified": check("skipped", "No live first-run verification requested.",
                                    "Run a job explicitly and inspect its artifacts."),
    }
