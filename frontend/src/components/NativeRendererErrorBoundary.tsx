import React from 'react';

interface Props {
  children: React.ReactNode;
  fallback: React.ReactNode;
  /** When this changes after a caught error, the boundary clears it and
   *  retries the children (e.g. a rebuilt renderer or a different file). */
  resetKey?: string;
}

interface State {
  hasError: boolean;
}

/**
 * Error boundary that catches render-time errors thrown by NativeRenderer
 * itself and falls back to the iframe renderer. A renderer module's mount
 * failures are handled inside NativeRenderer (its mountError panel), and
 * errors inside the module's own React root never reach this boundary.
 */
export class NativeRendererErrorBoundary extends React.Component<Props, State> {
  constructor(props: Props) {
    super(props);
    this.state = { hasError: false };
  }

  static getDerivedStateFromError(): State {
    return { hasError: true };
  }

  componentDidUpdate(prevProps: Props) {
    if (this.state.hasError && prevProps.resetKey !== this.props.resetKey) {
      this.setState({ hasError: false });
    }
  }

  componentDidCatch(error: Error, info: React.ErrorInfo) {
    console.error('NativeRenderer crashed, falling back to iframe:', error, info.componentStack);
  }

  render() {
    if (this.state.hasError) {
      return this.props.fallback;
    }
    return this.props.children;
  }
}
