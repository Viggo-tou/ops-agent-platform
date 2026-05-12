interface ReservationsBlockProps {
  reservations: string[];
}

export function ReservationsBlock({ reservations }: ReservationsBlockProps) {
  if (reservations.length === 0) {
    return null;
  }
  return (
    <div className="reservations-block" data-testid="reservations-block">
      <div className="reservations-block-header">
        <span className="reservations-block-icon" aria-hidden="true">
          ⚠️
        </span>
        <span className="reservations-block-title">
          保留意见与潜在风险（{reservations.length}）
        </span>
      </div>
      <ul className="reservations-block-list">
        {reservations.map((note, idx) => (
          <li key={idx} className="reservations-block-item">
            {note}
          </li>
        ))}
      </ul>
      <p className="reservations-block-footer">
        批准前请阅读以上提示。这些是 AI 评审给出的保留意见,不是强制阻塞。
      </p>
    </div>
  );
}
