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
    let pingInterval: ReturnType<typeof setInterval> | undefined

    ws.onopen = () => {
      setConnected(true)
      pingInterval = setInterval(() => {
        if (ws.readyState === WebSocket.OPEN) ws.send('ping')
      }, 30_000)
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
      clearInterval(pingInterval)
      setConnected(false)
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
