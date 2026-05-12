import { useNavigate } from "react-router-dom";

const cards = [
  {
    title: "AI 对话",
    body: "智能聊天，支持记忆与知识库",
    icon: "chat",
  },
  {
    title: "知识库",
    body: "上传文档，RAG 智能检索",
    icon: "book",
  },
  {
    title: "长期记忆",
    body: "自动提取与存储重要信息",
    icon: "brain",
  },
];

function HomeCardIcon({ type }: { type: string }) {
  if (type === "book") {
    return (
      <svg viewBox="0 0 24 24" aria-hidden="true">
        <path d="M4 5.5c0-.8.7-1.5 1.5-1.5H10c1.1 0 2 .9 2 2v14c0-1.1-.9-2-2-2H5.5C4.7 18 4 17.3 4 16.5v-11Z" />
        <path d="M20 5.5c0-.8-.7-1.5-1.5-1.5H14c-1.1 0-2 .9-2 2v14c0-1.1.9-2 2-2h4.5c.8 0 1.5-.7 1.5-1.5v-11Z" />
      </svg>
    );
  }
  if (type === "brain") {
    return (
      <svg viewBox="0 0 24 24" aria-hidden="true">
        <path d="M9 4a3 3 0 0 0-3 3 3 3 0 0 0-2 5.2A3.5 3.5 0 0 0 8 18h1V4Z" />
        <path d="M15 4a3 3 0 0 1 3 3 3 3 0 0 1 2 5.2A3.5 3.5 0 0 1 16 18h-1V4Z" />
        <path d="M9 8H7m2 4H6.5M15 8h2m-2 4h2.5" />
      </svg>
    );
  }
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <path d="M5 6.5A3.5 3.5 0 0 1 8.5 3h7A3.5 3.5 0 0 1 19 6.5v5A3.5 3.5 0 0 1 15.5 15H11l-4.5 4v-4A3.5 3.5 0 0 1 3 11.5v-5Z" />
    </svg>
  );
}

export function HomePage() {
  const navigate = useNavigate();

  return (
    <div className="home-page">
      <section className="home-hero">
        <h1>Knowledge Assistant</h1>
        <span>您的智能知识管理与学习助手</span>

        <div className="home-card-row">
          {cards.map((card) => (
            <article className="home-card" key={card.title}>
              <div className="home-card-icon">
                <HomeCardIcon type={card.icon} />
              </div>
              <h2>{card.title}</h2>
              <p>{card.body}</p>
            </article>
          ))}
        </div>

        <button className="home-start-button" type="button" onClick={() => void navigate("/chat")}>
          开始体验 →
        </button>
      </section>
    </div>
  );
}
