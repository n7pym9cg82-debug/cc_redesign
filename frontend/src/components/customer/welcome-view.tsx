import Image from "next/image";

function CutleryIcon() {
  return <svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor"
    strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
    <path d="M3.5 2.5v7a3 3 0 0 0 3 3v9" />
    <path d="M6.5 2.5v10M9.5 2.5v7a3 3 0 0 1-3 3" />
    <path d="M17.5 12.5c2 0 3.5-2.2 3.5-5s-1.5-5-3.5-5S14 4.7 14 7.5s1.5 5 3.5 5v9" />
  </svg>;
}

export function WelcomeView({ onEnter }: { onEnter: () => void }) {
  return <div className="welcome-stage">
    <h1 className="sr-only">Welcome to Chennai Café</h1>
    <Image className="welcome-art" src="/images/welcomePage.png" width={941} height={1672}
      sizes="(max-width: 480px) 100vw, 480px" priority
      alt="Chennai Café. Good food brings people together." />
    <div className="welcome-actions">
      <button className="welcome-primary" type="button" onClick={onEnter}>
        <span className="welcome-button-icon"><CutleryIcon /></span>
        <span>Start Your Order</span>
        <svg className="welcome-arrow" aria-hidden="true" viewBox="0 0 24 24" fill="none"
          stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="m9 5 7 7-7 7" />
        </svg>
      </button>
      <p className="welcome-helper">Tap to explore the menu</p>
    </div>
  </div>;
}
