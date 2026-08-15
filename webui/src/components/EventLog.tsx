// components/EventLog.tsx
'use client'

interface Props { events: string[] }

function eventColor(msg: string) {
  if (msg.includes('🛑') || msg.includes('❌')) return 'text-red-400'
  if (msg.includes('✅') || msg.includes('▶')) return 'text-green-400'
  if (msg.includes('⚡') || msg.includes('ARB')) return 'text-amber-400'
  if (msg.includes('📊') || msg.includes('MM')) return 'text-blue-400'
  if (msg.includes('🧠')) return 'text-purple-400'
  if (msg.includes('📄')) return 'text-cyan-400'
  if (msg.includes('⚠')) return 'text-amber-400'
  return 'text-[#8b91a8]'
}

export default function EventLog({ events }: Props) {
  return (
    <div className="card flex flex-col min-h-0">
      <div className="card-header">
        <span className="card-title">📜 Event Log</span>
        <span className="text-[11px] text-[#4a5068]">last {events.length}</span>
      </div>
      <div className="overflow-auto scrollbar-thin flex-1 px-3 py-2 space-y-0.5 font-mono text-[11.5px]">
        {events.length === 0 ? (
          <div className="text-[#4a5068] py-4 text-center text-xs">Waiting for events…</div>
        ) : (
          events.map((ev, i) => (
            <div key={i} className={`leading-5 ${eventColor(ev)}`}>{ev}</div>
          ))
        )}
      </div>
    </div>
  )
}
