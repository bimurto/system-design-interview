#!/usr/bin/env python3
"""
Reformat markdown tables so all cells in a column have the same width.
"""

import os
import re
from pathlib import Path


def parse_table(lines):
    """Parse a markdown table and return list of rows (each row is list of cells)."""
    rows = []
    for line in lines:
        line = line.strip()
        if not line.startswith('|'):
            break
        # Split by | and strip whitespace
        cells = [c.strip() for c in line.split('|')]
        # Remove empty first/last cells (from leading/trailing |)
        if cells and cells[0] == '':
            cells = cells[1:]
        if cells and cells[-1] == '':
            cells = cells[:-1]
        rows.append(cells)
    return rows


def is_separator_row(row):
    """Check if this is a separator row like |---|---|---|"""
    if not row:
        return False
    for cell in row:
        if not re.match(r'^-+$', cell):
            return False
    return True


def format_table(rows):
    """Format table with consistent column widths."""
    if not rows:
        return []

    # Determine max width for each column
    num_cols = max(len(row) for row in rows)
    col_widths = [0] * num_cols

    for row in rows:
        for i, cell in enumerate(row):
            if i < num_cols:
                col_widths[i] = max(col_widths[i], len(cell))

    # Format each row with padded cells
    formatted = []
    for i, row in enumerate(rows):
        # Pad cells to match column widths
        padded_cells = []
        for j in range(num_cols):
            if j < len(row):
                cell = row[j]
            else:
                cell = ''
            # For separator row, fill with dashes
            if is_separator_row([cell]) or (i == 1 and all(is_separator_row([r[j] if j < len(r) else '' for r in rows[1:2]]))):
                padded_cells.append('-' * col_widths[j])
            else:
                padded_cells.append(cell.ljust(col_widths[j]))
        formatted.append('| ' + ' | '.join(padded_cells) + ' |')

    return formatted


def process_file(filepath):
    """Process a single README file and reformat all tables."""
    with open(filepath, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    output_lines = []
    i = 0
    tables_found = 0

    while i < len(lines):
        line = lines[i]

        # Check if this starts a table
        if line.strip().startswith('|') and i + 1 < len(lines) and lines[i + 1].strip().startswith('|'):
            # Collect potential table lines
            table_start = i
            potential_table = []
            j = i
            while j < len(lines) and lines[j].strip().startswith('|'):
                potential_table.append(lines[j])
                j += 1

            # Parse and validate it's a proper table
            rows = parse_table(potential_table)

            if len(rows) >= 2 and is_separator_row(rows[1]):
                # Valid table - format it
                formatted = format_table(rows)
                output_lines.extend(formatted)
                output_lines.append('\n')  # Ensure newline after table
                tables_found += 1
                i = j
                # Skip blank line after table if present
                if i < len(lines) and lines[i].strip() == '':
                    i += 1
            else:
                # Not a valid table, keep original
                output_lines.append(line)
                i += 1
        else:
            output_lines.append(line)
            i += 1

    # Write back
    with open(filepath, 'w', encoding='utf-8') as f:
        f.writelines(output_lines)

    return tables_found


def main():
    base_path = Path(__file__).parent

    # Find all README.md files
    readme_files = []
    for root, dirs, files in os.walk(base_path):
        for f in files:
            if f == 'README.md':
                readme_files.append(Path(root) / f)

    total_tables = 0
    for filepath in sorted(readme_files):
        tables = process_file(filepath)
        if tables > 0:
            print(f"{filepath.relative_to(base_path)}: {tables} table(s)")
            total_tables += tables

    print(f"\nTotal: {total_tables} tables reformatted across {len(readme_files)} files")


if __name__ == '__main__':
    main()