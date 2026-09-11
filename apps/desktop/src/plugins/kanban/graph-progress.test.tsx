import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, expect, it } from 'vitest'

import { GraphProgress } from './graph-progress'

afterEach(cleanup)

it('shows persisted failure evidence and repeated attempts without declaring success', () => {
  render(
    <GraphProgress
      graph={{
        task_id: 'g',
        status: 'needs_attention',
        current_node: null,
        retries: 1,
        tokens: 120,
        duration_ms: 5000,
        nodes: [
          { node: 'executor', status: 'PASS', duration_ms: 1000, model: 'coding', reason: null },
          { node: 'reviewer', status: 'FAIL', duration_ms: 2000, model: 'review', reason: 'criterion failed' },
          { node: 'executor', status: 'failed', duration_ms: 2000, model: null, reason: 'budget exhausted' }
        ]
      }}
    />
  )
  expect(screen.getByRole('status').textContent).toContain('needs attention')
  expect(screen.getAllByText(/\d\. executor/)).toHaveLength(2)
  expect(screen.getByText('criterion failed')).toBeTruthy()
  expect(screen.getByText('budget exhausted')).toBeTruthy()
})
