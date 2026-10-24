/**
 * VIEW: Badge
 * ───────────
 * Status badge chip (Done, Processing, Waiting, Review, Leased, Flagged).
 */

import React from "react";

export function Badge({ type = "waiting", label }) {
  const normalized = (type || "waiting").toLowerCase();
  const text = label || type;

  let className = "r-badge r-badge--waiting";
  if (normalized === "done" || normalized === "finished") {
    className = "r-badge r-badge--done";
  } else if (normalized === "processing" || normalized === "running") {
    className = "r-badge r-badge--processing";
  } else if (normalized === "leased") {
    className = "r-badge r-badge--leased";
  } else if (normalized === "review" || normalized === "needs_review" || normalized === "failed") {
    className = "r-badge r-badge--review";
  } else if (normalized === "flagged") {
    className = "r-badge r-badge--flagged";
  }

  return <span className={className}>{text}</span>;
}
