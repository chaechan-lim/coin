import { useEffect, useRef, useState } from 'react'
import type { WsEvent } from '../types'

type EventHandler = (event: WsEvent) => void

export function useWebSocket(onMessage: EventHandler) {
  const wsRef = useRef<WebSocket | null>(null)
  const [connected, setConnected] = useState(false)
  const reconnectTimer = useRef<ReturnType<typeof setTimeout>>()
  const pingInterval = useRef<ReturnType<typeof setInterval>>()
  const onMessageRef = useRef(onMessage)

  // 항상 최신 콜백을 참조 (stale closure 방지)
  onMessageRef.current = onMessage

  useEffect(() => {
    function connect() {
      // 이전 ping interval 정리
      clearInterval(pingInterval.current)

      const protocol = window.location.protocol === 'https:' ? 'wss' : 'ws'
      const url = `${protocol}://${window.location.host}/ws/dashboard`
      const ws = new WebSocket(url)
      wsRef.current = ws

      ws.onopen = () => {
        setConnected(true)
        pingInterval.current = setInterval(() => {
          if (ws.readyState === WebSocket.OPEN) ws.send('ping')
        }, 30_000)
      }

      ws.onmessage = (e) => {
        try {
          const data = JSON.parse(e.data) as WsEvent
          onMessageRef.current(data)
        } catch {
          // ignore malformed messages
        }
      }

      ws.onclose = () => {
        clearInterval(pingInterval.current)
        setConnected(false)
        reconnectTimer.current = setTimeout(connect, 3_000)
      }

      ws.onerror = () => {
        ws.close()
      }
    }

    connect()
    return () => {
      clearTimeout(reconnectTimer.current)
      clearInterval(pingInterval.current)
      wsRef.current?.close()
    }
  }, [])

  return { connected }
}
