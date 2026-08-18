/**
 * ARIA - /supervise/shells/[name] — one shell's terminal.
 *
 * Keyed by name so switching shells remounts TerminalView and its stream
 * buffer rather than bleeding one shell's scrollback into another's.
 */
import { TerminalView } from '@/features/shells/TerminalView'

export default function ShellDetailPage({ params }: { params: { name: string } }) {
  const name = decodeURIComponent(params.name)
  return <TerminalView key={name} name={name} />
}
