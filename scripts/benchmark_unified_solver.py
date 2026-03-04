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
    <title>ProxLB Solver Benchmark: Model Comparison</title>
    <style>
        body { font-family: 'Segoe UI', system-ui, sans-serif; line-height: 1.6; margin: 0; background: #f1f5f9; color: #334155; }
        .container { max-width: 1200px; margin: 40px auto; padding: 0 20px; }
        .card { background: white; border-radius: 12px; box-shadow: 0 10px 15px -3px rgba(0,0,0,0.1); margin-bottom: 32px; padding: 32px; }
        h1 { color: #0f172a; margin-bottom: 8px; }
        h2 { color: #1e293b; margin-top: 0; border-bottom: 1px solid #e2e8f0; padding-bottom: 12px; }
        p { margin-bottom: 20px; }
        
        .intro-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 24px; margin-bottom: 32px; }
        .intro-box { background: #f8fafc; padding: 20px; border-radius: 8px; border-left: 4px solid #3b82f6; }
        .intro-box h4 { margin-top: 0; color: #2563eb; }

        .summary-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 20px; margin-bottom: 40px; }
        .stat-card { background: white; padding: 24px; border-radius: 12px; text-align: center; border-bottom: 4px solid #e2e8f0; }
        .stat-val { font-size: 2.5rem; font-weight: 800; color: #1e40af; display: block; }
        .stat-label { color: #64748b; text-transform: uppercase; font-size: 0.75rem; font-weight: 700; letter-spacing: 0.05em; }
        
        table { width: 100%; border-collapse: collapse; }
        th { background: #f8fafc; color: #475569; text-align: left; padding: 12px 16px; font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.05em; border-bottom: 2px solid #e2e8f0; }
        td { padding: 16px; border-bottom: 1px solid #f1f5f9; font-size: 0.875rem; }
        
        .winner-std { background: #eff6ff; }
        .winner-uni { background: #f5f3ff; }
        .status-pill { padding: 4px 12px; border-radius: 9999px; font-size: 0.75rem; font-weight: 600; }
        .bg-green { background: #dcfce7; color: #166534; }
        .bg-red { background: #fee2e2; color: #991b1b; }
        .bg-purple { background: #f3e8ff; color: #581c87; }
        
        .metric-label { color: #94a3b8; font-size: 0.75rem; margin-right: 4px; }
        .metric-val { font-weight: 600; color: #1e293b; }
        
        .legend { display: flex; gap: 20px; font-size: 0.8rem; margin-top: 10px; }
        .legend-item { display: flex; align-items: center; gap: 6px; }
        .dot { width: 12px; height: 12px; border-radius: 3px; }
    </style>
</head>
<body>
    <div class="container">
        <div class="card">
            <h1>Solver Comparison Report</h1>
            <p>This report compares two different mathematical approaches for VM rebalancing in Proxmox clusters.</p>
            
            <div class="intro-grid">
                <div class="intro-box">
                    <h4>Standard Model (Current)</h4>
                    <p>Uses a two-step approach: First, an optimizer finds the best final state. Second, a heuristic planner tries to find a path to get there. It is fast but can occasionally fail to find complex migration paths (Deadlocks).</p>
                </div>
                <div class="intro-box" style="border-left-color: #8b5cf6;">
                    <h4>Unified Model (New)</h4>
                    <p>Models the cluster across <strong>multiple time steps</strong> simultaneously. It proves reachability mathematically within the SAT problem. It is slower but finds more efficient paths and handles complex swaps naturally.</p>
                </div>
            </div>

            <h2>Glossary & Metrics</h2>
            <div class="legend">
                <div class="legend-item"><span class="metric-label">Load Gap:</span> Difference between most and least utilized node (Lower is better).</div>
                <div class="legend-item"><span class="metric-label">Migrations:</span> Number of VM moves required (Lower is better).</div>
                <div class="legend-item"><span class="metric-label">Slack:</span> Used when a cluster is 100% full to allow temporary overcommit during swaps.</div>
            </div>
        </div>

        <div class="summary-grid">
            <div class="stat-card"><span class="stat-val">{{ total }}</span><span class="stat-label">Scenarios</span></div>
            <div class="stat-card" style="border-bottom-color: #3b82f6;"><span class="stat-val">{{ std_wins }}</span><span class="stat-label">Standard Model Wins</span></div>
            <div class="stat-card" style="border-bottom-color: #8b5cf6;"><span class="stat-val">{{ uni_wins }}</span><span class="stat-label">Unified Model Wins</span></div>
            <div class="stat-card" style="border-bottom-color: #94a3b8;"><span class="stat-val">{{ draws }}</span><span class="stat-label">Equal Performance</span></div>
        </div>

        <div class="card">
            <h2>Scenario Results</h2>
            <table>
                <thead>
                    <tr>
                        <th>Scenario & Category</th>
                        <th>Standard Model</th>
                        <th>Unified Model</th>
                        <th>Key Comparison</th>
                        <th>Winner</th>
                    </tr>
                </thead>
                <tbody>
                    {% for r in results %}
                    <tr class="{{ 'winner-std' if 'Std' in r.verdict else 'winner-uni' if 'Uni' in r.verdict else '' }}">
                        <td>
                            <strong>{{ r.scenario }}</strong><br>
                            <span style="color: #94a3b8; font-size: 0.75rem;">{{ r.category }}</span>
                        </td>
                        <td>
                            <span class="status-pill {{ 'bg-green' if r.std_feasible else 'bg-red' }}">{{ 'Success' if r.std_feasible else 'Failed' }}</span>
                            <div style="margin-top: 8px;">
                                <span class="metric-label">Gap:</span><span class="metric-val">{{ r.std_gap|round(3) }}</span><br>
                                <span class="metric-label">Migs:</span><span class="metric-val">{{ r.std_migs }}</span>
                            </div>
                        </td>
                        <td>
                            <span class="status-pill {{ 'bg-green' if r.uni_feasible else 'bg-red' }}">{{ 'Success' if r.uni_feasible else 'Failed' }}</span>
                            {% if r.slack > 0 %}<span class="status-pill" style="background:#ffedd5; color:#9a3412; margin-left:4px;">Slack Used</span>{% endif %}
                            <div style="margin-top: 8px;">
                                <span class="metric-label">Gap:</span><span class="metric-val">{{ r.uni_gap|round(3) }}</span><br>
                                <span class="metric-label">Migs:</span><span class="metric-val">{{ r.uni_migs }}</span> <span style="color:#94a3b8">(T={{ r.uni_steps }})</span>
                            </div>
                        </td>
                        <td>
                            {% if r.std_feasible and r.uni_feasible %}
                                {% if r.std_gap - r.uni_gap > 0.001 %}
                                    <span style="color: #059669; font-weight: 600;">Unified balanced better</span>
                                {% elif r.uni_gap - r.std_gap > 0.001 %}
                                    <span style="color: #dc2626;">Standard balanced better</span>
                                {% elif r.std_migs > r.uni_migs %}
                                    <span style="color: #059669; font-weight: 600;">Unified is more efficient</span>
                                {% elif r.uni_migs > r.std_migs %}
                                    <span style="color: #dc2626;">Standard is more efficient</span>
                                {% else %}
                                    <span style="color: #64748b;">Identical Result</span>
                                {% endif %}
                            {% else %}
                                -
                            {% endif %}
                        </td>
                        <td>
                            {% if 'Uni' in r.verdict %}
                                <span class="status-pill bg-purple">Unified</span>
                            {% elif 'Std' in r.verdict %}
                                <span class="status-pill" style="background:#dbeafe; color:#1e40af;">Standard</span>
                            {% else %}
                                <span style="color: #94a3b8;">Draw</span>
                            {% endif %}
                        </td>
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
    # Find scenarios relative to this script
    base_dir = Path(__file__).resolve().parent.parent
    scenarios_dir = base_dir / "scenarios"
    files = sorted(list(scenarios_dir.rglob("*.yaml")))
    
    results = []
    
    if not files:
        print(f"Error: No scenarios found in {scenarios_dir}")
        return
        
    print(f"Generating Expert Report for {len(files)} scenarios...")
    
    uni_wins, std_wins, draws = 0, 0, 0

    for f in files:
        if "infeasible" in str(f): continue
        print(f"  Analysing {f.name}...", end="", flush=True)
        try:
            cluster = load_scenario(f)
            sol_std, _ = solve_reachable(cluster, quiet=True)
            sol_uni, _ = solve_unified(cluster, time_limit_s=10.0)
            
            # Comparison Logic
            verdict = "Draw"
            if not sol_uni.feasible and sol_std.feasible:
                verdict = "Std Wins"
                std_wins += 1
            elif sol_uni.feasible and not sol_std.feasible:
                verdict = "Uni Wins"
                uni_wins += 1
            elif sol_uni.feasible and sol_std.feasible:
                g_diff = sol_std.stats.load_gap - sol_uni.stats.load_gap
                if g_diff > 0.001:
                    verdict = "Uni Wins (Better Gap)"; uni_wins += 1
                elif g_diff < -0.001:
                    verdict = "Std Wins (Better Gap)"; std_wins += 1
                else:
                    # Gaps identical, compare migration count
                    m_diff = sol_std.stats.migration_count - sol_uni.stats.migration_count
                    if m_diff > 0: 
                        verdict = "Uni Wins (Fewer Migs)"; uni_wins += 1
                    elif m_diff < 0: 
                        verdict = "Std Wins (Fewer Migs)"; std_wins += 1
                    else: 
                        draws += 1
            else:
                draws += 1

            bench = sol_uni.stats.benchmark[-1] if sol_uni.stats.benchmark else {}
            results.append({
                "scenario": f.name, "category": f.parent.name,
                "std_feasible": sol_std.feasible, "std_gap": sol_std.stats.load_gap, "std_migs": sol_std.stats.migration_count,
                "uni_feasible": sol_uni.feasible, "uni_gap": sol_uni.stats.load_gap, "uni_migs": sol_uni.stats.migration_count,
                "uni_ms": sol_uni.stats.wall_time_ms, "uni_steps": bench.get("steps", 0), "slack": bench.get("slack", 0),
                "verdict": verdict
            })
            print(f" OK ({verdict})")
        except Exception as e:
            print(f" ERROR: {e}")

    print(f"\nFinal Stats: Std Wins={std_wins}, Uni Wins={uni_wins}, Draws={draws}")

    template = Template(HTML_TEMPLATE)
    html = template.render(results=results, total=len(results), std_wins=std_wins, uni_wins=uni_wins, draws=draws)
    with open("SOLVER_REPORT_EXPERT.html", "w") as f: f.write(html)
    print(f"\n[!] High-Quality Report generated: SOLVER_REPORT_EXPERT.html")

if __name__ == "__main__":
    benchmark()
