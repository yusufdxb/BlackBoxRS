"""Publisher sequence accounting in the flight report."""

from __future__ import annotations

from blackboxrs.flight.analysis import _seq_loss


def recs(seqs, epoch=1):
    return [{"data": {"seq": s, "epoch": epoch}} for s in seqs]


def test_in_order_run_has_no_loss():
    assert _seq_loss(recs([1, 2, 3, 4]), with_epoch=True)["lost"] == 0


def test_gap_is_loss():
    out = _seq_loss(recs([1, 2, 5, 6]), with_epoch=True)
    assert out["lost"] == 2 and out["reordered"] == 0


def test_swapped_pair_is_reordering_not_loss():
    out = _seq_loss(recs([1, 3, 2, 5, 4, 6]), with_epoch=True)
    assert out == {**out, "lost": 0, "reordered": 2, "duplicates": 0}


def test_redelivered_old_message_is_a_duplicate():
    out = _seq_loss(recs([1, 2, 3, 4, 2, 5]), with_epoch=True)
    assert out["duplicates"] == 1 and out["lost"] == 0 and out["reordered"] == 0


def test_epoch_change_is_a_restart_and_loss_is_per_run():
    r = recs([1, 2, 4]) + recs([1, 2, 3], epoch=2)
    out = _seq_loss(r, with_epoch=True)
    assert out["publisher_restarts"] == 1 and out["lost"] == 1


def test_counter_reset_without_epoch_is_a_restart():
    r = [{"data": {"seq": s}} for s in (5000, 5001, 1, 2)]
    out = _seq_loss(r, with_epoch=False)
    assert out["publisher_restarts"] == 1 and out["lost"] == 0 and out["reordered"] == 0
