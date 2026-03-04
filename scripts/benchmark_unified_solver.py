#!/usr/bin/env python3
import sys
import os
import time
from pathlib import Path
import pandas as pd
from jinja2 import Template

# Setup paths - ensure we can find the proxlb_solver module
base_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(base_dir))

from proxlb_solver.loader import load_scenario
from proxlb_solver.solver import solve_reachable
from proxlb_solver.unified_solver import solve_unified

HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>ProxLB Solver Benchmark</title>
    <style>
        body { font-family: 'Inter', system-ui, sans-serif; line-height: 1.5; margin: 0; background: #f8fafc; color: #1e293b; }
        .container { max-width: 1400px; margin: 40px auto; padding: 0 20px; }
        .card { background: white; border-radius: 12px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); margin-bottom: 24px; padding: 24px; }
        
        .summary-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 20px; margin-bottom: 32px; }
        .stat-card { background: white; padding: 20px; border-radius: 12px; text-align: center; border-top: 4px solid #3b82f6; }
        .stat-val { font-size: 2rem; font-weight: 800; color: #1d4ed8; display: block; }
        .stat-label { color: #64748b; text-transform: uppercase; font-size: 0.75rem; font-weight: 600; }
        
        table { width: 100%; border-collapse: collapse; background: white; border-radius: 12px; overflow: hidden; }
        th { background: #1e293b; color: white; text-align: left; padding: 12px 16px; font-size: 0.8rem; text-transform: uppercase; }
        td { padding: 12px 16px; border-bottom: 1px solid #f1f5f9; font-size: 0.85rem; vertical-align: top; }
        
        .winner-std { border-left: 4px solid #3b82f6; background: #eff6ff; }
        .winner-uni { border-left: 4px solid #8b5cf6; background: #f5f3ff; }
        
        .status-pill { padding: 4px 12px; border-radius: 9999px; font-size: 0.7rem; font-weight: 700; }
        .bg-green { background: #dcfce7; color: #166534; }
        .bg-red { background: #fee2e2; color: #991b1b; }
        .bg-orange { background: #ffedd5; color: #9a3412; }
        .expected-fail { background: #f1f5f9; color: #64748b; border: 1px dashed #cbd5e1; }
    </style>
</head>
<body>
    <div class="container">
        <div class="card">
            <h1>ProxLB Solver Comparison Report</h1>
            <p><strong>Standard Model</strong> vs <strong>Unified SAT Model</strong>. Highlighting reachability and quality.</p>
        </div>

        <div class="summary-grid">
            <div class="stat-card"><span class="stat-val">{{ total }}</span><span class="stat-label">Scenarios</span></div>
            <div class="stat-card" style="border-top-color:#3b82f6"><span class="stat-val">{{ std_wins }}</span><span class="stat-label">Std Wins</span></div>
            <div class="stat-card" style="border-top-color:#8b5cf6"><span class="stat-val">{{ uni_wins }}</span><span class="stat-label">Uni Wins</span></div>
            <div class="stat-card" style="border-top-color:#94a3b8"><span class="stat-val">{{ draws }}</span><span class="stat-label">Draws</span></div>
        </div>

        <div class="card">
            <table>
                <thead>
                    <tr>
                        <th>Scenario</th>
                        <th>Standard Model</th>
                        <th>Unified Model</th>
                        <th>Winner / Verdict</th>
                    </tr>
                </thead>
                <tbody>
                    {% for r in results %}
                    <tr class="{{ r.verdict_class }}">
                        <td>
                            <strong>{{ r.scenario }}</strong><br>
                            <small>{{ r.category }}</small>
                            {% if not r.expected_feasible %}
                                <br><span class="status-pill expected-fail">Expected to Fail</span>
                            {% endif %}
                        </td>
                        <td>
                            {% set std_c = 'bg-green' if r.std_feasible else 'bg-red' %}
                            {% if not r.expected_feasible and r.std_feasible %}{% set std_c = 'bg-orange' %}{% endif %}
                            <span class="status-pill {{ std_c }}">{{ 'Success' if r.std_feasible else 'Infeasible' }}</span>
                            <br><small>Gap: {{ r.std_gap|round(3) }}, Migs: {{ r.std_migs }}</small>
                        </td>
                        <td>
                            {% set uni_c = 'bg-green' if r.uni_feasible else 'bg-red' %}
                            {% if not r.expected_feasible and r.uni_feasible %}{% set uni_c = 'bg-orange' %}{% endif %}
                            <span class="status-pill {{ uni_c }}">{{ 'Success' if r.uni_feasible else 'Infeasible' }}</span>
                            {% if r.slack > 0 %}<br><small style="color:orange">Used {{ r.slack }} MiB Slack</small>{% endif %}
                            <br><small>Gap: {{ r.uni_gap|round(3) }}, Migs: {{ r.uni_migs }} (T={{ r.uni_steps }})</small>
                        </td>
                        <td><strong>{{ r.verdict }}</strong></td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
        </div>
    </div>
</body>
</html>
"""

def benchmark():
    base_dir = Path(__file__).resolve().parent.parent
    scenarios_dir = base_dir / "scenarios"
    files = sorted(list(scenarios_dir.rglob("*.yaml")))
    results = []
    uni_wins, std_wins, draws = 0, 0, 0

    for f in files:
        print(f"  Analysing {f.name}...", end="", flush=True)
        try:
            cluster = load_scenario(f)
            expected_feasible = cluster.expect.feasible
            sol_std, _ = solve_reachable(cluster, quiet=True)
            sol_uni, _ = solve_unified(cluster, time_limit_s=10.0)
            
            verdict, verdict_class = "Draw", "draw"
            
            if not expected_feasible:
                if not sol_uni.feasible and not sol_std.feasible:
                    verdict = "Both correctly failed"
                elif sol_uni.feasible and not sol_std.feasible:
                    verdict = "Uni Fake Success!"; verdict_class = "winner-std"; std_wins += 1
                elif not sol_uni.feasible and sol_std.feasible:
                    verdict = "Std Fake Success!"; verdict_class = "winner-uni"; uni_wins += 1
            else:
                if not sol_uni.feasible and sol_std.feasible:
                    verdict = "Std Wins (Uni Failed)"; verdict_class = "winner-std"; std_wins += 1
                elif sol_uni.feasible and not sol_std.feasible:
                    verdict = "Uni Wins (Std Failed)"; verdict_class = "winner-uni"; uni_wins += 1
                elif sol_uni.feasible and sol_std.feasible:
                    g_diff = sol_std.stats.load_gap - sol_uni.stats.load_gap
                    if g_diff > 0.001: verdict = "Uni Wins (Better Gap)"; verdict_class = "winner-uni"; uni_wins += 1
                    elif g_diff < -0.001: verdict = "Std Wins (Better Gap)"; verdict_class = "winner-std"; std_wins += 1
                    else:
                        m_diff = sol_std.stats.migration_count - sol_uni.stats.migration_count
                        if m_diff > 0: verdict = "Uni Wins (Fewer Migs)"; verdict_class = "winner-uni"; uni_wins += 1
                        elif m_diff < 0: verdict = "Std Wins (Fewer Migs)"; verdict_class = "winner-std"; std_wins += 1
                        else: draws += 1
                else: draws += 1

            bench = sol_uni.stats.benchmark[-1] if sol_uni.stats.benchmark else {}
            results.append({
                "scenario": f.name, "category": f.parent.name, "expected_feasible": expected_feasible,
                "std_feasible": sol_std.feasible, "std_gap": sol_std.stats.load_gap, "std_migs": sol_std.stats.migration_count,
                "uni_feasible": sol_uni.feasible, "uni_gap": sol_uni.stats.load_gap, "uni_migs": sol_uni.stats.migration_count,
                "uni_ms": sol_uni.stats.wall_time_ms, "uni_steps": bench.get("steps", 0), "slack": bench.get("slack", 0),
                "uni_vars": bench.get("variables", 0), "uni_cons": bench.get("constraints", 0),
                "verdict": verdict, "verdict_class": verdict_class
            })
            print(" OK")
        except Exception as e: print(f" ERROR: {e}")

    template = Template(HTML_TEMPLATE)
    html = template.render(results=results, total=len(results), std_wins=std_wins, uni_wins=uni_wins, draws=draws)
    with open(base_dir / "SOLVER_REPORT_EXPERT.html", "w") as f: f.write(html)
    print(f"\n[!] Expert Report generated: SOLVER_REPORT_EXPERT.html")

if __name__ == "__main__":
    benchmark()
