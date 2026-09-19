// Window → tab → pane state as serialized by the server's build_state().
export interface PaneRect { x: number; y: number; w: number; h: number; }
export interface Session { id: string; name: string; rect?: PaneRect; }
export interface Tab { index: number; id: string; title?: string; isSelected: boolean; currentSessionId?: string; aspect?: number; maximized?: boolean; sessions: Session[]; }
export interface Bounds { x: number; y: number; w: number; h: number; }
export interface WindowState { id: string; isFront: boolean; tabs: Tab[]; bounds?: Bounds; }
export interface ScreenSize { width: number; height: number; }
