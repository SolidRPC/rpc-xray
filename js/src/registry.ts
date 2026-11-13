export const BUCKETS = [0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10];

const MAX_SERIES = 5000;

type Labels = Record<string, string>;

interface Histogram {
  labels: Labels;
  counts: number[];
  sum: number;
  count: number;
}

function escapeValue(value: string): string {
  return value.replace(/\\/g, "\\\\").replace(/"/g, '\\"').replace(/\n/g, "\\n");
}

function renderLabels(labels: Labels, extra?: Labels): string {
  const pairs = Object.entries({ ...labels, ...extra }).map(
    ([key, value]) => `${key}="${escapeValue(value)}"`,
  );
  return pairs.length > 0 ? `{${pairs.join(",")}}` : "";
}

function keyOf(labels: Labels): string {
  return Object.values(labels).join("\u0001");
}

export interface GaugeSample {
  name: string;
  help: string;
  labels: Labels;
  value: number;
}

export class Registry {
  private counters = new Map<string, { help: string; series: Map<string, [Labels, number]> }>();
  private histograms = new Map<string, { help: string; series: Map<string, Histogram> }>();
  private seriesCount = 0;

  inc(name: string, help: string, labels: Labels, value = 1): void {
    let family = this.counters.get(name);
    if (!family) {
      family = { help, series: new Map() };
      this.counters.set(name, family);
    }
    const key = keyOf(labels);
    const existing = family.series.get(key);
    if (existing) {
      existing[1] += value;
      return;
    }
    if (this.seriesCount >= MAX_SERIES) return;
    this.seriesCount += 1;
    family.series.set(key, [labels, value]);
  }

  observe(name: string, help: string, labels: Labels, value: number): void {
    let family = this.histograms.get(name);
    if (!family) {
      family = { help, series: new Map() };
      this.histograms.set(name, family);
    }
    const key = keyOf(labels);
    let histogram = family.series.get(key);
    if (!histogram) {
      if (this.seriesCount >= MAX_SERIES) return;
      this.seriesCount += 1;
      histogram = { labels, counts: BUCKETS.map(() => 0), sum: 0, count: 0 };
      family.series.set(key, histogram);
    }
    for (let index = 0; index < BUCKETS.length; index += 1) {
      if (value <= (BUCKETS[index] as number)) (histogram.counts[index] as number) += 1;
    }
    histogram.sum += value;
    histogram.count += 1;
  }

  render(gauges: GaugeSample[]): string {
    const lines: string[] = [];
    for (const [name, family] of this.counters) {
      lines.push(`# HELP ${name} ${family.help}`, `# TYPE ${name} counter`);
      for (const [labels, value] of family.series.values()) {
        lines.push(`${name}${renderLabels(labels)} ${value}`);
      }
    }
    for (const [name, family] of this.histograms) {
      lines.push(`# HELP ${name} ${family.help}`, `# TYPE ${name} histogram`);
      for (const histogram of family.series.values()) {
        BUCKETS.forEach((bucket, index) => {
          lines.push(
            `${name}_bucket${renderLabels(histogram.labels, { le: String(bucket) })} ${histogram.counts[index]}`,
          );
        });
        lines.push(
          `${name}_bucket${renderLabels(histogram.labels, { le: "+Inf" })} ${histogram.count}`,
          `${name}_sum${renderLabels(histogram.labels)} ${histogram.sum}`,
          `${name}_count${renderLabels(histogram.labels)} ${histogram.count}`,
        );
      }
    }
    const seen = new Set<string>();
    for (const gauge of gauges) {
      if (!seen.has(gauge.name)) {
        seen.add(gauge.name);
        lines.push(`# HELP ${gauge.name} ${gauge.help}`, `# TYPE ${gauge.name} gauge`);
      }
      lines.push(`${gauge.name}${renderLabels(gauge.labels)} ${gauge.value}`);
    }
    return `${lines.join("\n")}\n`;
  }
}
