# Apply rate by finder (Claude vs. Perplexity) across the weekly Claude search
# windows, computed from the search_findings telemetry table.
#
# Input: a local SQLite snapshot of the Turso database — the pipeline never
# writes one; export it yourself (e.g. `turso db shell <db> .dump | sqlite3
# snapshot.db`) and point JSA_ANALYSIS_DB at it. Run from the repo root, where
# .Rprofile activates renv; the plot lands in analysis/ (gitignored).

library(DBI)
library(RSQLite)
library(dplyr)
library(lubridate)
library(tidyr)
library(ggplot2)
library(scales)
library(purrr)

# 1. Connect to the local SQLite snapshot
con <- dbConnect(SQLite(), Sys.getenv("JSA_ANALYSIS_DB", "~/Downloads/job-search-agent.db"))

# 2. Reference the table lazily and collect into an in-memory tibble
search_df <- dbReadTable(con, "search_findings") |>
  as_tibble() |>
  mutate(
    end_date = as.Date(run_date),
    start_date = end_date - days(window_hours / 24),
  )

# 3. Disconnect when finished
dbDisconnect(con)

# 4. Identify Claude search windows and find overlapping Perplexity runs
claude_windows <- search_df |>
  filter(agent == "claude") |>
  distinct(start_date, end_date, window_hours) |>
  arrange(start_date) |>
  filter(window_hours == 168) # Only keep 7-day windows for now

overlap_summary <- map(seq_len(nrow(claude_windows)), \(i) {
  start <- claude_windows[i, ]$start_date
  end <- claude_windows[i, ]$end_date

  claude_urls <- search_df |>
    filter(agent == "claude", start_date == start, end_date == end) |>
    filter(decision %in% c("Apply", "Skip")) |>
    distinct(canonical_url, decision)

  perplexity_urls <- search_df |>
    filter(
      agent == "perplexity",
      as.Date(start_date) >= as.Date(start),
      as.Date(end_date) <= as.Date(end)
    ) |>
    filter(decision %in% c("Apply", "Skip")) |>
    distinct(canonical_url, decision)

  # Classify each distinct URL in this window by who found it
  all_urls <- bind_rows(
    claude_urls |> mutate(source = "claude"),
    perplexity_urls |> mutate(source = "perplexity")
  ) |>
    summarize(
      finder = case_when(
        all(c("claude", "perplexity") %in% source) ~ "Both",
        "claude" %in% source ~ "Claude Only",
        TRUE ~ "Perplexity Only"
      ),
      decision = first(decision),
      .by = canonical_url
    )

  all_urls |>
    summarize(
      period_label = paste0(start, " to ", end),
      n_total = n(),
      n_apply = sum(decision == "Apply"),
      apply_rate = n_apply / n_total,
      .by = finder
    )
}) |>
  bind_rows()

# 5. Format factors and plot
plot_data <- overlap_summary |>
  mutate(
    finder = factor(
      finder,
      levels = c("Both", "Claude Only", "Perplexity Only")
    )
  )

ggplot(plot_data, aes(x = apply_rate, y = period_label, fill = finder)) +
  geom_col(width = 0.6) +
  geom_text(
    aes(
      label = ifelse(
        n_total > 0,
        paste0(
          percent(apply_rate, accuracy = 1),
          " (",
          n_apply,
          "/",
          n_total,
          ")"
        ),
        ""
      ),
      hjust = ifelse(apply_rate > 0.55, 1.08, -0.08),
      color = ifelse(apply_rate > 0.55, "inside", "outside")
    ),
    size = 3.0,
    show.legend = FALSE
  ) +
  facet_wrap(vars(finder)) +
  scale_color_manual(
    values = c("inside" = "white", "outside" = "grey20")
  ) +
  scale_x_continuous(
    labels = label_percent(),
    limits = c(0, 1),
    expand = expansion(mult = c(0, 0.28))
  ) +
  coord_cartesian(clip = "off") +
  scale_fill_manual(
    values = c(
      "Both" = "#5E81AC",
      "Claude Only" = "#D08770",
      "Perplexity Only" = "#A3BE8C"
    )
  ) +
  labs(
    title = "Apply Rate by Finder Across Search Periods",
    subtitle = "% of postings decided 'Apply' (with count of apply / total found)",
    x = "Apply Rate (%)",
    y = NULL
  ) +
  theme_minimal(base_size = 11) +
  theme(
    plot.title.position = "plot",
    plot.title = element_text(face = "bold", size = 13),
    plot.subtitle = element_text(color = "grey35", margin = margin(b = 8)),
    legend.position = "none",
    axis.text.y = element_text(size = 9, color = "grey20"),
    panel.grid.minor = element_blank(),
    panel.grid.major.y = element_blank(),
    plot.margin = margin(t = 10, r = 16, b = 10, l = 10)
  )

# 6. Export plot to PNG
ggsave(
  filename = "analysis/apply_rate_by_finder.png",
  width = 11,
  height = 6,
  dpi = 300,
  bg = "white"
)
