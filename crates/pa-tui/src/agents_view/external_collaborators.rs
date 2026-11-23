//! Read-only collaborator rows and recent exchanges in the Agents view.
use crate::external_collaborators::{Direction, Snapshot};

use super::{AgentsViewMode, AgentsViewRow, Line, RowKind, Section, ThemeColor, Value};

impl Snapshot {
    fn identity(&self) -> String {
        format!("external:{}:{}", self.owner_session_id, self.id)
    }

    fn row(&self, parent: Option<&AgentsViewRow>) -> AgentsViewRow {
        AgentsViewRow {
            kind: RowKind::ExternalCollaborator,
            section: parent.map_or(Section::Inactive, |row| row.section),
            identity: self.identity(),
            parent_identity: parent.map(|row| row.identity.clone()),
            // Deliberately no native session identifiers, even the owner's.
            summary: Value::Null,
            title: format!(
                "{} · last reported: {} · {}{}",
                self.provider,
                self.status,
                self.name,
                self.activity
                    .as_ref()
                    .map_or(String::new(), |activity| format!(" · {activity}")),
            ),
            model: String::new(),
            cost: 0.0,
            age: String::new(),
            depth: parent.map_or(0, |row| row.depth + 1),
            descendant_count: 0,
            running_subagent_count: 0,
            expanded: false,
            has_spawn_code: false,
        }
    }

    fn matches(&self, query: &str) -> bool {
        let fields = format!("{} {} {}", self.name, self.provider, self.status).to_lowercase();
        query
            .split_whitespace()
            .all(|part| fields.contains(&part.to_lowercase()))
    }
}

pub(super) struct Exchange {
    identity: String,
    scroll: usize,
}

impl AgentsViewMode {
    pub(super) fn scoped_owner_id(&self) -> Option<&str> {
        let scope = self.options.scope.as_ref().filter(|_| self.scope_active)?;
        scope.session_id.as_deref().or_else(|| {
            let active = scope.active_session_id.as_deref()?;
            self.roster.iter().find_map(|entry| {
                let summary = entry.get("summary")?;
                (summary.get("activeSessionId")?.as_str()? == active)
                    .then(|| summary.get("sessionId")?.as_str())
                    .flatten()
            })
        })
    }

    pub(super) fn external_owners(&self) -> Vec<String> {
        let mut owners = Vec::new();
        if let Some(owner) = self.scoped_owner_id() {
            owners.push(owner.to_owned());
        }
        for row in &self.rows {
            if matches!(row.kind, RowKind::Agent | RowKind::Subagent) {
                if let Some(id) = row.summary.get("sessionId").and_then(Value::as_str) {
                    if !owners.iter().any(|owner| owner == id) {
                        owners.push(id.to_owned());
                    }
                }
            }
        }
        owners
    }

    pub(super) fn external_rows(&self, rows: Vec<AgentsViewRow>) -> Vec<AgentsViewRow> {
        if self.external_snapshots.is_empty() {
            return rows;
        }
        let jobs = |owner: Option<&str>| {
            owner
                .and_then(|id| self.external_snapshots.get(id))
                .into_iter()
                .flatten()
        };
        let mut result: Vec<_> = jobs(self.scoped_owner_id())
            .filter(|job| job.matches(&self.query))
            .map(|job| job.row(None))
            .collect();
        for row in rows {
            let children = if matches!(row.kind, RowKind::Agent | RowKind::Subagent) {
                jobs(row.summary.get("sessionId").and_then(Value::as_str))
                    .filter(|job| job.matches(&self.query))
                    .map(|job| job.row(Some(&row)))
                    .collect()
            } else {
                Vec::new()
            };
            result.push(row);
            result.extend(children);
        }
        result
    }

    pub(super) fn open_exchange(&mut self, row: &AgentsViewRow) {
        self.external_exchange = Some(Exchange {
            identity: row.identity.clone(),
            scroll: 0,
        });
    }

    pub(super) fn scroll_exchange(&mut self, delta: isize) {
        if let Some(exchange) = &mut self.external_exchange {
            exchange.scroll = exchange.scroll.saturating_add_signed(delta);
        }
    }

    pub(super) fn handle_exchange_key(&mut self, key: &str) -> bool {
        if self.external_exchange.is_none() {
            return false;
        }
        let matches = |id| self.keybindings.matches(key, id);
        if matches("tui.select.cancel") || matches("app.agents.back") || matches("app.clear") {
            self.external_exchange = None;
        } else if matches("tui.select.up") {
            self.scroll_exchange(-1);
        } else if matches("tui.select.down") {
            self.scroll_exchange(1);
        } else if matches("tui.select.pageUp") {
            self.scroll_exchange(-(self.last_height.saturating_sub(3).max(1) as isize));
        } else if matches("tui.select.pageDown") {
            self.scroll_exchange(self.last_height.saturating_sub(3).max(1) as isize);
        }
        true
    }

    pub(super) fn render_exchange(&mut self, width: usize, height: usize) -> Option<Vec<Line>> {
        let exchange = self.external_exchange.as_mut()?;
        let mut lines = Vec::new();
        if width == 0 || height == 0 {
            return Some(lines);
        }
        let job = self
            .external_snapshots
            .values()
            .flatten()
            .find(|job| job.identity() == exchange.identity);
        let mut body = Vec::new();
        let title = if let Some(job) = job {
            let age_secs = (crate::agents_view_state::now_ms() as f64 / 1_000.0 - job.updated_at)
                .max(0.0) as u64;
            let observed = if age_secs >= 60 {
                format!("{}m ago", age_secs / 60)
            } else {
                format!("{age_secs}s ago")
            };
            let detail = format!("Last reported: {} ({observed})", job.status);
            body.extend(crate::width::wrap_text(&detail, width));
            if let Some(activity) = &job.activity {
                body.extend(crate::width::wrap_text(activity, width));
            }
            for message in &job.messages {
                let sender = match message.direction {
                    Direction::ToExternal => "Prime".to_owned(),
                    Direction::FromExternal => job.provider.clone(),
                };
                body.push(Vec::new());
                let label = if message.status.is_empty() {
                    sender
                } else {
                    format!("{sender} · {}", message.status)
                };
                let label = vec![self.theme.fg(ThemeColor::Warning, label)];
                body.extend(crate::width::wrap_line(&label, width));
                body.extend(crate::width::wrap_text(&message.text, width));
            }
            if job.messages.is_empty() {
                body.push(vec![self
                    .theme
                    .fg(ThemeColor::Dim, "No messages reported".to_owned())]);
            }
            format!("{} · {} · recent exchange", job.name, job.provider)
        } else {
            "External exchange no longer available".to_owned()
        };
        let clip = |text: &str| super::truncate_text(text, width);
        lines.push(vec![self.theme.fg(ThemeColor::Text, clip(&title))]);
        if height == 1 {
            return Some(lines);
        }
        let viewport = height.saturating_sub(2);
        exchange.scroll = exchange.scroll.min(body.len().saturating_sub(viewport));
        lines.extend(body.into_iter().skip(exchange.scroll).take(viewport));
        lines.resize_with(height - 1, Vec::new);
        let hint = format!(
            "{}/{} scroll   {} back",
            self.keybindings.key_text("tui.select.up"),
            self.keybindings.key_text("tui.select.down"),
            self.keybindings.key_text("tui.select.cancel")
        );
        lines.push(vec![self.theme.fg(ThemeColor::Muted, clip(&hint))]);
        Some(lines)
    }
}
