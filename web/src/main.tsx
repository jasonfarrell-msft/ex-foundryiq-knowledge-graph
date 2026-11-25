import { createRoot } from 'react-dom/client';
import { ErrorBoundary } from 'react-error-boundary';
import { App } from './App';
import './styles.css';

createRoot(document.getElementById('root')!).render(
  <ErrorBoundary fallback={
    <main className="fatal-error" role="alert">
      <span className="eyebrow">SPEECH ATLAS</span>
      <h1>The workspace could not be displayed.</h1>
      <p>Reload to start a new session. No question will be sent automatically.</p>
      <button className="ask-button" type="button" onClick={() => window.location.reload()}>Reload workspace</button>
    </main>
  }><App /></ErrorBoundary>,
);
