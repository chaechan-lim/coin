import { Component, type ReactNode } from 'react'

interface Props {
  children: ReactNode
}

interface State {
  error: Error | null
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null }

  static getDerivedStateFromError(error: Error) {
    return { error }
  }

  render() {
    if (this.state.error) {
      return (
        <div className="min-h-screen bg-gray-900 text-white flex items-center justify-center p-6">
          <div className="bg-gray-800 rounded-xl p-6 max-w-lg w-full space-y-4">
            <h2 className="text-red-400 font-bold text-lg">렌더링 오류</h2>
            <pre className="text-xs text-gray-400 whitespace-pre-wrap break-all bg-gray-900 rounded p-3">
              {this.state.error.message}
              {'\n\n'}
              {this.state.error.stack}
            </pre>
            <button
              onClick={() => {
                this.setState({ error: null })
                window.location.reload()
              }}
              className="px-4 py-2 bg-blue-600 rounded text-sm font-medium hover:bg-blue-500"
            >
              새로고침
            </button>
          </div>
        </div>
      )
    }
    return this.props.children
  }
}
