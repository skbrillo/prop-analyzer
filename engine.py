reverse=True
    )

    SITE.mkdir(exist_ok=True)

    payload = {
        "generated_at": (
            datetime.datetime.now(
                datetime.timezone.utc
            ).isoformat()
        ),

        "mode": (
            "REAL DATA. Experimental probability audit. "
            "Probabilities blend an uncertainty-adjusted normal approximation "
            "with no-vig DraftKings market probabilities. Whole-number lines "
            "include estimated push probability. Injury and lineup changes "
            "require verified inputs. These probabilities are NOT calibrated "
            "and EV is NOT a validated betting advantage."
        ),

        "results": output,
    }

    (SITE / "results.json").write_text(
        json.dumps(payload, indent=2),
        encoding="utf-8",
    )

    print("ESPN player game logs:", len(logs))
    print("DraftKings player prop lines:", len(odds))
    print("Team defense game records:", len(defense))
    print("Generated prediction outcomes:", len(output))
    print("Players matched to teams:", len(matched))
    print("Players without verified teams:", len(unmatched))

    print(
        "Props using baseline because minutes unavailable:",
        no_minutes,
    )

    print(
        "Props with team defense applied:",
        defense_applied,
    )

    print(
        "Props with neutral defense fallback:",
        defense_fallback,
    )

    print(
        "Injury-confirmed out props skipped:",
        skipped_out,
    )

    analyzed_props = len(output) // 2
    total_rejected = sum(rejection_counts.values())

    print("Props analyzed:", analyzed_props)
    print("Props rejected:", total_rejected)

    for reason, count in rejection_counts.items():
        print(f"Skipped - {reason}: {count}")

    print(
        "Prop accounting check:",
        f"{analyzed_props} analyzed + "
        f"{total_rejected} rejected = "
        f"{analyzed_props + total_rejected} / {len(odds)}",
    )

    diagnostics_path = DATA / "rejected_props.csv"

    with diagnostics_path.open(
        "w",
        newline="",
        encoding="utf-8",
    ) as file:
        writer = csv.DictWriter(
            file,
            fieldnames=[
                "game_date",
                "player",
                "market",
                "line",
                "matchup",
                "reason",
            ],
        )

        writer.writeheader()
        writer.writerows(rejected_props)

    print(
        "Rejected prop details saved:",
        diagnostics_path,
    )

    if unmatched:
        print(
            "Unmatched players:",
            ", ".join(sorted(unmatched)),
        )


if __name__ == "__main__":
    run()
