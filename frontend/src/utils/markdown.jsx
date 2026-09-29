// ENMA chat markdown rendering — extracted from App.jsx and
// extended. Supports: headings, paragraphs, ordered/unordered
// (nested-by-indent) lists, tables, blockquotes, inline code,
// bold, and SAFE external links (markdown-form and bare URLs).
//
// Safety: only http/https URLs ever become links. javascript:,
// file:, data: and every other scheme stay inert plain text.

import React from "react";

const SAFE_URL_PATTERN = /^https?:\/\/[^\s<>")'\]]+$/i;

export function isSafeHttpUrl(url) {
  return typeof url === "string" && SAFE_URL_PATTERN.test(url);
}

// Order matters: code spans first (protect their contents),
// then markdown links, then bare URLs.
const INLINE_TOKEN_PATTERN =
  /(`[^`\n]+`)|(\[[^\]\n]+\]\(https?:\/\/[^\s)]+\))|(https?:\/\/[^\s<>")'\]]+)/g;

function renderBoldAndText(text, keyBase) {
  // Bold pass over a plain (already link/code-free) segment.
  const nodes = [];
  const boldPattern = /\*\*[^*\n]+\*\*/g;
  let lastIndex = 0;
  let match;
  let key = 0;

  while ((match = boldPattern.exec(text)) !== null) {
    if (match.index > lastIndex) {
      nodes.push(text.slice(lastIndex, match.index));
    }
    nodes.push(
      <strong key={`${keyBase}-b${key++}`}>
        {match[0].slice(2, -2)}
      </strong>
    );
    lastIndex = match.index + match[0].length;
  }
  if (lastIndex < text.length) {
    nodes.push(text.slice(lastIndex));
  }
  return nodes;
}

export function renderInline(text) {
  const nodes = [];
  let lastIndex = 0;
  let match;
  let key = 0;

  INLINE_TOKEN_PATTERN.lastIndex = 0;

  while ((match = INLINE_TOKEN_PATTERN.exec(text)) !== null) {
    if (match.index > lastIndex) {
      nodes.push(
        ...renderBoldAndText(
          text.slice(lastIndex, match.index),
          `pre-${key}`
        )
      );
    }

    if (match[1] !== undefined) {
      // Inline code span.
      nodes.push(
        <code className="ghost-inline-code" key={`code-${key}`}>
          {match[1].slice(1, -1)}
        </code>
      );
    } else if (match[2] !== undefined) {
      // Markdown link [text](https://...).
      const closeParen = match[2].lastIndexOf("](");
      const label = match[2].slice(1, closeParen);
      const url = match[2].slice(closeParen + 2, -1);

      if (isSafeHttpUrl(url)) {
        nodes.push(
          <a
            className="ghost-link"
            key={`link-${key}`}
            href={url}
            target="_blank"
            rel="noopener noreferrer"
          >
            {label}
          </a>
        );
      } else {
        nodes.push(label);
      }
    } else if (match[3] !== undefined) {
      // Bare URL; trailing sentence punctuation stays prose.
      let url = match[3];
      let trailing = "";
      const punct = /[.,;:!?]+$/;
      const m = url.match(punct);
      if (m) {
        trailing = m[0];
        url = url.slice(0, -trailing.length);
      }
      if (isSafeHttpUrl(url)) {
        nodes.push(
          <a
            className="ghost-link ghost-link-bare"
            key={`link-${key}`}
            href={url}
            target="_blank"
            rel="noopener noreferrer"
          >
            {url}
          </a>
        );
        if (trailing) nodes.push(trailing);
      } else {
        nodes.push(url);
      }
    }

    key += 1;
    lastIndex = match.index + match[0].length;
  }

  if (lastIndex < text.length) {
    nodes.push(...renderBoldAndText(text.slice(lastIndex), `tail`));
  }

  return nodes;
}

// ------------------------------------------------------------
// Tables: consecutive lines starting with "|" where the second
// line is a |---|---| separator.
// ------------------------------------------------------------

function isTableLine(line) {
  const t = line.trim();
  return t.startsWith("|") && t.endsWith("|") && t.includes("|", 1);
}

function isTableSeparator(line) {
  const t = line.trim();
  return /^\|(\s*:?-{2,}:?\s*\|)+$/.test(t);
}

function splitRow(line) {
  return line
    .trim()
    .replace(/^\|/, "")
    .replace(/\|$/, "")
    .split("|")
    .map((cell) => cell.trim());
}

function renderTable(lines, keyBase) {
  const header = splitRow(lines[0]);
  const rows = lines.slice(2).map(splitRow);

  return (
    <table className="ghost-table" key={`${keyBase}-table`}>
      <thead>
        <tr>
          {header.map((cell, i) => (
            <th key={i}>{renderInline(cell)}</th>
          ))}
        </tr>
      </thead>
      <tbody>
        {rows.map((row, r) => (
          <tr key={r}>
            {header.map((_, c) => (
              <td key={c}>{renderInline(row[c] ?? "")}</td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  );
}

// ------------------------------------------------------------
// One non-code text block -> structured JSX.
// ------------------------------------------------------------

export function renderTextBlock(block, keyBase = "b") {
  const lines = block.split("\n");
  const out = [];
  let i = 0;
  let k = 0;

  while (i < lines.length) {
    const line = lines[i];
    const value = line.trim();

    if (!value) {
      out.push(<div key={`${keyBase}-sp${k++}`} className="ghost-spacer" />);
      i += 1;
      continue;
    }

    // Table group.
    if (isTableLine(value) && i + 1 < lines.length) {
      const group = [];
      let j = i;
      while (
        j < lines.length &&
        lines[j].trim() &&
        (isTableLine(lines[j].trim()) || isTableSeparator(lines[j].trim()))
      ) {
        group.push(lines[j]);
        j += 1;
      }
      if (group.length >= 2 && isTableSeparator(group[1].trim())) {
        out.push(renderTable(group, `${keyBase}-t${k++}`));
        i = j;
        continue;
      }
      // Not a real table (missing separator): fall through as text.
    }

    if (value.startsWith("### ")) {
      out.push(<h4 key={`${keyBase}-h${k++}`}>{renderInline(value.slice(4))}</h4>);
      i += 1;
      continue;
    }
    if (value.startsWith("## ")) {
      out.push(<h3 key={`${keyBase}-h${k++}`}>{renderInline(value.slice(3))}</h3>);
      i += 1;
      continue;
    }
    if (value.startsWith("# ")) {
      out.push(<h2 key={`${keyBase}-h${k++}`}>{renderInline(value.slice(2))}</h2>);
      i += 1;
      continue;
    }

    if (/^[-*•]\s+/.test(value)) {
      const indent = line.match(/^\s*/)[0].length;
      out.push(
        <div
          key={`${keyBase}-ul${k++}`}
          className="ghost-bullet"
          style={indent >= 2 ? { marginLeft: indent * 8 } : undefined}
        >
          <span>◆</span>
          <span>{renderInline(value.replace(/^[-*•]\s+/, ""))}</span>
        </div>
      );
      i += 1;
      continue;
    }

    const numbered = value.match(/^(\d+)[.)]\s+(.*)$/);
    if (numbered) {
      // The number stays visually attached to its content.
      out.push(
        <div key={`${keyBase}-ol${k++}`} className="ghost-numbered">
          <span className="ghost-numbered-marker">{numbered[1]}.</span>
          <span className="ghost-numbered-content">
            {renderInline(numbered[2])}
          </span>
        </div>
      );
      i += 1;
      continue;
    }

    if (value.startsWith("> ")) {
      out.push(
        <blockquote key={`${keyBase}-q${k++}`} className="ghost-quote">
          {renderInline(value.slice(2))}
        </blockquote>
      );
      i += 1;
      continue;
    }

    out.push(<p key={`${keyBase}-p${k++}`}>{renderInline(value)}</p>);
    i += 1;
  }

  return out;
}
