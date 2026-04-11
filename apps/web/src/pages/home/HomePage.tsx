import { Link } from "react-router-dom";

const cards = [
  {
    title: "AI chat",
    body: "Ask questions, plan work, and continue recent conversations.",
  },
  {
    title: "Knowledge",
    body: "Upload documents and retrieve grounded repository evidence.",
  },
  {
    title: "Long memory",
    body: "Keep durable preferences while controlling sensitive topics.",
  },
];

export function HomePage() {
  return (
    <div className="home-page">
      <section className="home-hero">
        <p>Knowledge Assistant</p>
        <h1>Knowledge Assistant</h1>
        <span>Your knowledge and learning assistant.</span>

        <div className="home-card-row">
          {cards.map((card) => (
            <article className="home-card" key={card.title}>
              <div className="home-card-icon">{card.title.slice(0, 1)}</div>
              <h2>{card.title}</h2>
              <p>{card.body}</p>
            </article>
          ))}
        </div>

        <Link className="home-start-button" to="/chat">
          Start
        </Link>
      </section>
    </div>
  );
}
