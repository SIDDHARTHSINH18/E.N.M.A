import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/react';
import TaskCard, { lifecycleActionFor } from './TaskCard';

// vitest globals are off, so testing-library's automatic DOM
// cleanup does not fire on its own.
afterEach(cleanup);

// TaskCard renders the POST /api/tasks envelope: the task id
// lives at task.task.id and the status on task.task.status /
// task.execution.state.
function makeTask(status, id = 'task-1') {
  return {
    task: { id, title: 'Check whether the file exists', status },
    planning: { ready: true, steps: [{}] },
    execution: { state: status, steps: [] },
  };
}

describe('lifecycleActionFor', () => {
  it('maps PENDING and RUNNING to cancel', () => {
    expect(lifecycleActionFor('PENDING')).toBe('cancel');
    expect(lifecycleActionFor('RUNNING')).toBe('cancel');
  });

  it('maps FAILED to retry', () => {
    expect(lifecycleActionFor('FAILED')).toBe('retry');
  });

  it('maps terminal states to no action', () => {
    expect(lifecycleActionFor('COMPLETED')).toBeNull();
    expect(lifecycleActionFor('CANCELLED')).toBeNull();
  });
});

describe('TaskCard lifecycle actions', () => {
  it('offers Cancel on a RUNNING task and calls it with the task id', () => {
    const onTaskAction = vi.fn().mockResolvedValue(undefined);
    render(<TaskCard task={makeTask('RUNNING')} onTaskAction={onTaskAction} />);

    const button = screen.getByRole('button', { name: 'Cancel task' });
    fireEvent.click(button);

    expect(onTaskAction).toHaveBeenCalledTimes(1);
    expect(onTaskAction).toHaveBeenCalledWith('cancel', 'task-1', null);
  });

  it('offers Cancel on a PENDING task', () => {
    const onTaskAction = vi.fn().mockResolvedValue(undefined);
    render(<TaskCard task={makeTask('PENDING')} onTaskAction={onTaskAction} />);

    expect(screen.getByRole('button', { name: 'Cancel task' })).toBeTruthy();
  });

  it('offers Retry on a FAILED task and calls it with the task id', () => {
    const onTaskAction = vi.fn().mockResolvedValue(undefined);
    render(<TaskCard task={makeTask('FAILED')} onTaskAction={onTaskAction} />);

    fireEvent.click(screen.getByRole('button', { name: 'Retry task' }));

    expect(onTaskAction).toHaveBeenCalledTimes(1);
    expect(onTaskAction).toHaveBeenCalledWith('retry', 'task-1', null);
  });

  it('offers no lifecycle action on COMPLETED and CANCELLED tasks', () => {
    const onTaskAction = vi.fn();

    for (const status of ['COMPLETED', 'CANCELLED']) {
      const { unmount } = render(
        <TaskCard task={makeTask(status)} onTaskAction={onTaskAction} />,
      );
      expect(screen.queryByRole('button', { name: 'Cancel task' })).toBeNull();
      expect(screen.queryByRole('button', { name: 'Retry task' })).toBeNull();
      unmount();
    }

    expect(onTaskAction).not.toHaveBeenCalled();
  });

  it('shows the refreshed status after the action resolves', async () => {
    const onTaskAction = vi.fn().mockImplementation(async () => {
      // The App-level handler refreshes state through GET
      // /tasks/{id}; here we simulate the re-render with the
      // resulting status.
      rerender(<TaskCard task={makeTask('CANCELLED')} onTaskAction={onTaskAction} />);
    });

    const { rerender } = render(
      <TaskCard task={makeTask('RUNNING')} onTaskAction={onTaskAction} />,
    );

    fireEvent.click(screen.getByRole('button', { name: 'Cancel task' }));

    expect(screen.queryByRole('button', { name: 'Cancel task' })).toBeNull();
    expect(screen.getByText('Cancelled')).toBeTruthy();
  });

  it('shows an error note when the action fails', async () => {
    const onTaskAction = vi
      .fn()
      .mockRejectedValue(new Error('API request failed: 409'));

    render(<TaskCard task={makeTask('FAILED')} onTaskAction={onTaskAction} />);

    fireEvent.click(screen.getByRole('button', { name: 'Retry task' }));

    const note = await screen.findByText(/Retry failed/);
    expect(note.textContent).toContain('409');
  });
});
