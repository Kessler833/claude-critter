export type Mood = {
  mode: string
  tool: string
  detail: string
  typing: number
  text: string
  event: string
  eventAt: number
  tokens?: number
  spit?: string
  dur?: number
  act?: string
  limit?: number
  agents: { id: string; model: string; t?: number }[]
}

declare module 'claude-code' {
  interface PluginState {
    critter: { mood: Mood }
  }
}
