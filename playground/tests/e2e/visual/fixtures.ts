export const EXPECTED_VISUAL_COUNT = 3;
export const FIXTURES: Record<string, { filename: string; content: string; marker: string }> = {
  text: {
    filename: 'fixture.txt',
    content: 'Visual fixture: notes\n\nThree ideas to compare:\n  1. Read the source\n  2. Capture the output\n  3. Review the baseline\n',
    marker: 'Visual fixture: notes',
  },
  table: {
    filename: 'fixture.csv',
    content: 'Idea,Score,Status\nRead the source,9,Complete\nCapture the output,8,Review\nReview the baseline,10,Ready\n',
    marker: 'Read the source',
  },
  schema_form: {
    filename: 'fixture.set_yaml',
    content: 'fields:\n  - key: title\n    label: Project title\n    type: text\n  - key: enabled\n    label: Enabled\n    type: checkbox\n  - key: count\n    label: Candidates\n    type: number\nvalues:\n  title: Visual fixture\n  enabled: true\n  count: 3\n',
    marker: 'Project title',
  },
};
