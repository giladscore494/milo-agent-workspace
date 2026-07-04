from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from . import core

EventSink = Callable[[str, dict[str, Any]], None]


@dataclass
class VehicleCatalogRunConfig:
    api_key: str
    manufacturer: str = core.DEFAULT_MANUFACTURER
    market: str = core.DEFAULT_MARKET
    period: str = core.DEFAULT_PERIOD


@dataclass
class VehicleCatalogEngine:
    model_client_factory: Callable[[str, str], Any] | None = None
    sleep_fn: Callable[[float], None] | None = None
    event_sink: EventSink | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    _previous_client_factory: Any = field(default=None, init=False)
    _previous_sleep_fn: Any = field(default=None, init=False)

    def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        if self.event_sink:
            self.event_sink(event_type, payload)

    def _add_tokens(self, result: dict[str, Any]) -> None:
        self.input_tokens += int(result.get("input_tokens", 0) or 0)
        self.output_tokens += int(result.get("output_tokens", 0) or 0)

    def _install_injections(self) -> None:
        self._previous_client_factory = core.MODEL_CLIENT_FACTORY
        self._previous_sleep_fn = core.SLEEP_FN
        if self.model_client_factory is not None:
            core.MODEL_CLIENT_FACTORY = self.model_client_factory
        if self.sleep_fn is not None:
            core.SLEEP_FN = self.sleep_fn

    def _restore_injections(self) -> None:
        core.MODEL_CLIENT_FACTORY = self._previous_client_factory
        core.SLEEP_FN = self._previous_sleep_fn

    def run(self, config: VehicleCatalogRunConfig) -> dict[str, Any]:
        start = time.perf_counter()
        self.input_tokens = 0
        self.output_tokens = 0
        results: dict[str, Any] = {}
        failed_summaries: list[dict[str, Any]] = []
        self._install_injections()
        try:
            self._emit("phase_started", {"phase": "discovery"})
            discovery_results = core.run_discovery_phase(config.api_key, config.manufacturer, config.market, config.period)
            for r in discovery_results:
                self._add_tokens(r)
            results["discovery_phase"] = discovery_results
            self._emit("phase_completed", {"phase": "discovery", "results": discovery_results})
            successful_discovery = [r for r in discovery_results if r["status"] == "success"]
            if not successful_discovery:
                reason = discovery_results[0].get("error") if discovery_results else "NO_DISCOVERY_RESULTS"
                return self._failed(results, reason, "Discovery failed", start)

            merged = core.merge_discovery_candidates(discovery_results)
            results["python_discovery_merge"] = merged

            self._emit("phase_started", {"phase": "normalizer"})
            normalizer = core.run_normalizer_phase(config.api_key, merged)
            self._add_tokens(normalizer)
            results["normalizer_deduper"] = normalizer
            self._emit("phase_completed", {"phase": "normalizer", "result": normalizer})
            if normalizer["status"] != "success":
                return self._failed(results, normalizer.get("error", "NORMALIZER_FAILED"), "Normalizer failed", start)
            canonical_models = normalizer["parsed"].get("canonical_models", [])

            self._emit("phase_started", {"phase": "technical_enrichment"})
            technical_results = core.run_technical_enrichment_phase(config.api_key, config.manufacturer, config.market, config.period, canonical_models)
            technical_clean: dict[str, Any] = {}
            for r in technical_results:
                self._add_tokens(r)
                if r["status"] in {"success", "partial"} and isinstance(r.get("parsed"), dict):
                    technical_clean[r["agent"]] = r["parsed"]
                else:
                    failed_summaries.append({"agent": r["agent"], "error": r.get("error"), "message": r.get("message", "")})
            results["technical_enrichment_phase"] = technical_results
            self._emit("phase_completed", {"phase": "technical_enrichment", "results": technical_results})
            if not technical_clean:
                return self._failed(results, "TECHNICAL_ENRICHMENT_FAILED", "all technical enrichment agents failed", start)

            self._emit("phase_started", {"phase": "verification"})
            verifier = core.run_verification_phase(config.api_key, normalizer["parsed"], technical_clean, failed_summaries)
            self._add_tokens(verifier)
            results["source_verifier"] = verifier
            if verifier["status"] == "success":
                verifier_data = verifier["parsed"]
                verifier_data["status"] = "success"
            elif verifier["status"] == "partial" and isinstance(verifier.get("parsed"), dict):
                failed_summaries.append({"agent": "source_verifier", "error": verifier.get("error"), "message": verifier.get("message", "")})
                failed_summaries.extend(verifier.get("failed_chunks", []))
                verifier_data = verifier["parsed"]
                verifier_data["status"] = "partial"
            else:
                failed_summaries.append({"agent": "source_verifier", "error": verifier.get("error"), "message": verifier.get("message", "")})
                verifier_data = {"agent": "source_verifier", "status": "failed", "verified_models": [], "rejected_data_points": [], "needs_review": [{"model": "*", "reason": verifier.get("error")}]} 
            self._emit("phase_completed", {"phase": "verification", "result": verifier})

            final = core.run_final_builder_phase(config.api_key, normalizer["parsed"], technical_clean, verifier_data, failed_summaries, config.manufacturer, config.market, config.period)
            self._add_tokens(final)
            results["final_builder"] = final
            if final["status"] != "success":
                return self._failed(results, final.get("error", "FINAL_BUILDER_FAILED"), "Final builder failed", start)

            summary = core.run_hebrew_summary_phase(config.api_key, final["parsed"])
            self._add_tokens(summary)
            results["hebrew_summary"] = summary
            status = final["parsed"].get("status", "success")
            return {"status": status, "result": final["parsed"], "summary": summary.get("parsed", {}).get("summary") if summary.get("status") == "success" else None, "results": results, "input_tokens": self.input_tokens, "output_tokens": self.output_tokens, "elapsed_seconds": time.perf_counter() - start}
        finally:
            self._restore_injections()

    def _failed(self, results: dict[str, Any], code: str, message: str, start: float) -> dict[str, Any]:
        return {"status": "failed", "error": {"code": code, "message": message}, "results": results, "input_tokens": self.input_tokens, "output_tokens": self.output_tokens, "elapsed_seconds": time.perf_counter() - start}
