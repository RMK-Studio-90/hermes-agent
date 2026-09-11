import { compactNumber } from '@hermes/plugin-sdk'

export interface GraphProgressData {
  task_id: string
  status: string
  current_node: null | string
  retries: number
  tokens: number
  duration_ms: number
  nodes: { node: string; status: string; duration_ms: null | number; model: null | string; reason: null | string }[]
}

export function GraphProgress({ graph }: { graph: GraphProgressData }) {
  return (
    <div aria-label="Hermes execution graph" className="space-y-3 text-[0.8125rem] text-(--ui-text-secondary)">
      <p role="status">
        {graph.status.replaceAll('_', ' ')} · {compactNumber(graph.tokens)} tokens ·{' '}
        {Math.round(graph.duration_ms / 1000)} s · {graph.retries} repairs
      </p>
      <ol className="space-y-2">
        {graph.nodes.map((node, index) => (
          <li className="flex flex-wrap items-baseline gap-x-3" key={`${node.node}-${index}`}>
            <span className="text-(--ui-text-primary)">
              {index + 1}. {node.node.replaceAll('_', ' ')}
            </span>
            <span>{node.status.replaceAll('_', ' ')}</span>
            {node.model && <span>{node.model}</span>}
            {node.duration_ms !== null && <span>{Math.round(node.duration_ms / 1000)} s</span>}
            {node.reason && <p className="w-full">{node.reason}</p>}
          </li>
        ))}
      </ol>
      {graph.current_node && <p>Next: {graph.current_node.replaceAll('_', ' ')}</p>}
    </div>
  )
}
