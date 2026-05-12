interface ReviewVerdictBadgeProps {
  verdict: string | null;
}

export function ReviewVerdictBadge({ verdict }: ReviewVerdictBadgeProps) {
  if (!verdict) {
    return <span className="pill review-unknown">no review</span>;
  }

  return <span className={`pill review-${verdict}`}>{verdict.replace(/_/g, " ")}</span>;
}
