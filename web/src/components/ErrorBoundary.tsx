import { Component, type ErrorInfo, type ReactNode } from 'react'
import styles from './ErrorBoundary.module.css'

interface ErrorBoundaryProps {
  children: ReactNode
}

interface ErrorBoundaryState {
  hasError: boolean
}

/** Catches render/lifecycle errors anywhere below it and shows a fallback
 * instead of an unrecoverable white screen. Must be a class component --
 * componentDidCatch/getDerivedStateFromError have no hook equivalent. */
export class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  state: ErrorBoundaryState = { hasError: false }

  static getDerivedStateFromError(): ErrorBoundaryState {
    return { hasError: true }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error('Shady Stroll crashed:', error, info.componentStack)
  }

  render() {
    if (this.state.hasError) {
      return (
        // role="alert" (assertive) is deliberate here, unlike RouteStats'
        // aria-live="polite" -- this replaces the whole app, so it should
        // interrupt rather than wait politely.
        <div className={styles.fallback} role="alert">
          <p className={styles.heading}>// APP_CRASHED</p>
          <p className={styles.message}>Something went wrong and Shady Stroll can't recover on its own.</p>
          <button type="button" className={styles.reloadButton} onClick={() => window.location.reload()}>
            RELOAD
          </button>
        </div>
      )
    }
    return this.props.children
  }
}
