"""Basic-combat evidence is separate from RSI promotion and full-match wins."""
ATTACKS = {'attack_move', 'farm_wave', 'melee', 'q', 'e', 'r'}


def effect(skill, feedback):
    scores = feedback['score_delta']
    attack = skill in ATTACKS
    return {'attack_command_completed': attack,
            'own_last_hit_observed': attack and scores['creepScore'] > 0,
            'own_champion_kill_observed': attack and scores['kills'] > 0,
            'damage_amount': None,
            'interpretation': 'own_score_change_during_action_window_not_damage_measurement'}


def summarize(observations, report):
    eligible = (report.get('origin') in ('lol_windows_live', 'lol_windows_diagnostic')
        and report.get('input_released') is True and report.get('assisted') is False
        and report.get('recording_started_before_input') is True
        and report.get('video', {}).get('decode_verified') is True
        and report.get('video', {}).get('window_only') is True
        and report.get('video', {}).get('microphone') is False
        and not report.get('video', {}).get('recorder_error'))
    hits = [row['call_slot'] for row in observations if row['effect']['own_last_hit_observed']
            and row['skill'] == 'farm_wave' and row.get('visual_target_selected') is True]
    return {'schema': 1, 'observed_actions': len(observations),
            'attack_commands_completed': sum(r['effect']['attack_command_completed'] for r in observations),
            'targeted_last_hit_slots': hits,
            'recorded_targeted_last_hit': eligible and bool(hits),
            'full_combat_acceptance_passed': False,
            'reason': 'requires_independent_frame_video_and_response_review',
            'hero_combat_verified': False, 'rsi_promotion': False}
