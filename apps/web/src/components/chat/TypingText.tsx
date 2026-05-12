import { useEffect, useState } from "react";

interface TypingTextProps {
  text: string;
  speed?: number;
  enabled?: boolean;
}

export function TypingText({ text, speed = 18, enabled = true }: TypingTextProps) {
  const [visibleCount, setVisibleCount] = useState(enabled ? 0 : text.length);

  useEffect(() => {
    if (!enabled) {
      setVisibleCount(text.length);
      return;
    }

    setVisibleCount(0);
    const intervalId = window.setInterval(() => {
      setVisibleCount((currentCount) => {
        if (currentCount >= text.length) {
          window.clearInterval(intervalId);
          return text.length;
        }
        return currentCount + 1;
      });
    }, speed);

    return () => window.clearInterval(intervalId);
  }, [enabled, speed, text]);

  if (!enabled || visibleCount >= text.length) {
    return <>{text}</>;
  }

  return (
    <>
      {text.slice(0, visibleCount)}
      <span className="typing-cursor" />
    </>
  );
}
