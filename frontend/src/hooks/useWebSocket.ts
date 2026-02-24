import { useEffect, useRef, useCallback, useState } from 'react'
import type { WsEvent } from '../types'

type EventHandler = (event: WsEvent) => void

export function useWebSocket(onMessage: EventHandler) {
  const wsRef = useRef<WebSocket | null>(null)
  const [connected, setConnected] = useState(false)
  const reconnectTimer = useRef<ReturnType<typeof setTimeout>>()

  const connect = useCallback(() => {
    const protocol = window.location.protocol === 'https:' ? 'wss' : 'ws'
    const url = `${protocol}://${window.location.host}/ws/dashboard`
    const ws = new WebSocket(url)
    wsRef.current = ws

    ws.onopen = () => {
      setConnected(true)
      // Send ping every 30s to keep alive
      const pingInterval = setInterval(() => {
        if (ws.readyState === WebSocket.OPEN) ws.send('ping')
      }, 30_000)
      ws.onclose = () => clearInterval(pingInterval)
    }

    ws.onmessage = (e) => {
      try {
        const data = JSON.parse(e.data) as WsEvent
        onMessage(data)
      } catch {
        // ignore malformed messages
      }
    }

    ws.onclose = () => {
      setConnected(false)
      // Reconnect after 3s
      reconnectTimer.current = setTimeout(connect, 3_000)
    }

    ws.onerror = () => {
      ws.close()
    }
  }, [onMessage])

  useEffect(() => {
    connect()
    return () => {
      clearTimeout(reconnectTimer.current)
      wsRef.current?.close()
    }
  }, [connect])

  return { connected }
}
