"""Arithmetic and calibration isolation for finite-window curve working models."""
from __future__ import annotations

import copy
import json
import math
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np
from scipy.special import ndtri

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'research/liquidity_aware_hedging'))
from orderbook import LabError
from orderbook.book_shape import BookShapeConfig
from orderbook.shape_curve_models import (FEATURE_NAMES, GEOMETRY_METRICS, _family_cdf,
                                          _fit_candidate, _profile, analyze_curve_models,
                                          curve_geometry, enrich_curve_rows)


def row(distances=(1., 2., 3., 4., 5.), weights=None, ask_scale=1., time=0):
    distances = np.asarray(distances, dtype=np.float64)
    weights = np.asarray(weights if weights is not None else np.full(len(distances), 1. / len(distances)),
                         dtype=np.float64)
    return dict(usable=True, session_id='1', session_date='2026-10-09', unix_us=time,
                partition='calibration', regime='tight', _bid_distances=distances,
                _bid_weights=weights, _ask_distances=distances * ask_scale,
                _ask_weights=weights.copy(), depth_imbalance=.2)


class ShapeCurveModelTests(unittest.TestCase):
    def test_linear_hand_arithmetic_and_matching_secants(self):
        weights = np.full(4, .25)
        x = np.cumsum(weights)
        geometry = curve_geometry(2. + 3. * x, weights)
        self.assertAlmostEqual(geometry['linear_slope_bps'], 3.)
        self.assertAlmostEqual(geometry['linear_r2'], 1.)
        self.assertLess(geometry['linear_rmse_bps'], 1e-12)
        self.assertAlmostEqual(geometry['quadratic_curvature_bps'], 0.)
        self.assertAlmostEqual(geometry['near_secant_bps'], 3.)
        self.assertAlmostEqual(geometry['far_secant_bps'], 3.)
        self.assertAlmostEqual(geometry['secant_change_bps'], 0.)

    def test_quadratic_slope_at_observed_center_and_curvature(self):
        weights = np.full(4, .25)
        x = np.cumsum(weights)
        geometry = curve_geometry(1. + 2. * x + 4. * x**2, weights)
        self.assertAlmostEqual(geometry['quadratic_slope_bps'], 2. + 8. * np.mean(x))
        self.assertAlmostEqual(geometry['quadratic_curvature_bps'], 8.)
        self.assertAlmostEqual(geometry['quadratic_min_derivative_bps'], 2. + 8. * x[0])
        self.assertAlmostEqual(geometry['quadratic_r2'], 1.)
        self.assertLess(geometry['quadratic_rmse_bps'], 1e-12)
        self.assertGreater(geometry['linear_rmse_bps'], .1)
        self.assertGreater(geometry['secant_change_bps'], 0)

    def test_two_levels_leave_quadratic_unavailable(self):
        geometry = curve_geometry([1., 3.], [.25, .75])
        self.assertAlmostEqual(geometry['linear_slope_bps'], 2. / .75)
        self.assertEqual(geometry['near_secant_bps'], geometry['far_secant_bps'])
        self.assertTrue(all(geometry[key] is None for key in GEOMETRY_METRICS if key.startswith('quadratic')))

    def test_numerically_unsupported_fraction_fit_is_explicitly_missing(self):
        geometry = curve_geometry([1., 2., 3.], [1. - 2e-15, 1e-15, 1e-15])
        self.assertIsNone(geometry['linear_slope_bps'])
        self.assertIsNone(geometry['quadratic_slope_bps'])
        self.assertIsNone(geometry['near_secant_bps'])
        self.assertIsNone(geometry['far_secant_bps'])

    def test_rejects_invalid_distance_weight_and_fixed_window(self):
        for distances, weights in (([0., 1.], [.5, .5]), ([1., 1.], [.5, .5]),
                                   ([2., 1.], [.5, .5]), ([1., 2.], [.5, 0.]),
                                   ([1., math.inf], [.5, .5]), ([1., 2.], [.2, .2]),
                                   ([1.], [1.]), ([[1., 2.]], [[.5, .5]])):
            with self.subTest(distances=distances, weights=weights), self.assertRaises(LabError):
                curve_geometry(distances, weights)
        with self.assertRaises(LabError):
            analyze_curve_models([row(distances=[1., 2.])], [], BookShapeConfig(levels=3))

    def test_enrichment_preserves_labels_replay_inputs_and_adds_export_columns(self):
        data = row(ask_scale=2.)
        original = copy.deepcopy(data)
        enrich_curve_rows([data])
        self.assertTrue(all(key in data for key in FEATURE_NAMES))
        self.assertEqual(set(data) - set(original), set(FEATURE_NAMES))
        for key in ('regime', 'partition', 'session_date', 'unix_us', 'depth_imbalance'):
            self.assertEqual(data[key], original[key])
        np.testing.assert_array_equal(data['_bid_distances'], original['_bid_distances'])
        self.assertAlmostEqual(data['curve_asymmetry_linear_slope_bps'], -data['bid_curve_linear_slope_bps'])

    def test_mean_cdf_gives_each_snapshot_equal_weight(self):
        samples = [row([1., 2.], [.9, .1]), row([1., 2.], [.1, .9])]
        profile = _profile(samples, 'bid', np.array([0., 1., 2.]))
        np.testing.assert_allclose(profile['cdf_mean'], [0., .5, 1.])
        np.testing.assert_allclose(profile['mass_mean'], [0., .5, .5])
        self.assertEqual(profile['outside_range_mass_mean'], 0.)

    def test_heldout_tail_is_not_clipped_or_renormalized(self):
        train = [row(time=i) for i in range(3)]
        test = [row([1., 2., 3., 4., 10.], [.1, .1, .1, .1, .6], time=10)]
        report = analyze_curve_models(train, test, BookShapeConfig())
        heldout = report['spatial_cdf_models']['bid']['test']
        self.assertAlmostEqual(heldout['cdf_mean'][-1], .4)
        self.assertAlmostEqual(sum(heldout['mass_mean']), .4)
        self.assertAlmostEqual(heldout['above_range_mass_mean'], .6)
        self.assertAlmostEqual(heldout['outside_range_mass_mean'], .6)
        self.assertEqual(report['spatial_cdf_models']['edges_bps'][-1], 5.)

    def test_parameters_edges_and_training_scores_invariant_to_test_changes(self):
        train = [row(time=i, ask_scale=1.2) for i in range(5)]
        first = analyze_curve_models(copy.deepcopy(train), [row(time=10)], BookShapeConfig())
        second = analyze_curve_models(copy.deepcopy(train), [row(np.arange(1., 6.) * 100., time=10)], BookShapeConfig())
        self.assertEqual(first['spatial_cdf_models']['edges_bps'], second['spatial_cdf_models']['edges_bps'])
        for side in ('bid', 'ask'):
            for left, right in zip(first['spatial_cdf_models'][side]['candidates'],
                                   second['spatial_cdf_models'][side]['candidates']):
                for key in ('parameters', 'parameter_bounds', 'cdf', 'training_rmse', 'training_mae',
                            'status', 'iterations', 'bounds_hit'):
                    self.assertEqual(left[key], right[key])
            self.assertNotEqual(first['spatial_cdf_models'][side]['test']['cdf_mean'],
                                second['spatial_cdf_models'][side]['test']['cdf_mean'])

    def test_known_candidate_cdf_recovers_parameters_and_frozen_error(self):
        edges = np.linspace(0., 10., 65)
        train = [row(time=i) for i in range(3)]
        for family, parameters, expected in (
                ('lognormal', [.2, .8], dict(mu_log_bps=.2, sigma_log_bps=.8)),
                ('weibull', [math.log(1.5), math.log(2.)], dict(shape=1.5, scale_bps=2.))):
            with self.subTest(family=family):
                cdf = _family_cdf(family, parameters, edges)
                profile = dict(samples=3, cdf_mean=cdf.tolist(), mass_mean=np.diff(np.r_[0., cdf]).tolist())
                fit = _fit_candidate(family, train, 'bid', edges, profile, profile)
                self.assertTrue(fit['converged'])
                for key, value in expected.items():
                    self.assertAlmostEqual(fit['parameters'][key], value, places=5)
                self.assertLess(fit['training_rmse'], 1e-7)
                self.assertEqual(fit['test_rmse'], fit['training_rmse'])
                self.assertAlmostEqual(fit['model_tail_mass_above_range'], 1. - cdf[-1], places=7)

    def test_sampled_lognormal_profile_has_small_working_fit_error(self):
        probabilities = (np.arange(50) + .5) / 50
        distances = np.exp(.3 + .6 * ndtri(probabilities))
        train = [row(distances=distances, time=i) for i in range(4)]
        report = analyze_curve_models(train, [row(distances=distances, time=10)], BookShapeConfig(levels=50))
        fit = report['spatial_cdf_models']['bid']['candidates'][0]
        self.assertEqual(fit['family'], 'lognormal')
        self.assertTrue(fit['converged'])
        self.assertAlmostEqual(fit['parameters']['mu_log_bps'], .3, delta=.025)
        self.assertAlmostEqual(fit['parameters']['sigma_log_bps'], .6, delta=.025)
        self.assertLess(fit['test_rmse'], .011)

    def test_nonconvergence_and_failed_fit_remain_visible(self):
        train = [row(time=i) for i in range(3)]
        frozen = SimpleNamespace(x=np.array([0., 1.]), active_mask=np.array([0, 0]),
                                 success=False, nfev=500)
        with patch('orderbook.shape_curve_models.least_squares', return_value=frozen):
            report = analyze_curve_models(train, [], BookShapeConfig())
        fit = report['spatial_cdf_models']['bid']['candidates'][0]
        self.assertEqual(fit['status'], 'nonconverged')
        self.assertFalse(fit['converged'])
        self.assertIsNone(fit['parameters'])
        self.assertIsNone(fit['training_rmse'])
        self.assertIsNone(fit['test_rmse'])
        self.assertEqual(fit['cdf'], [])
        self.assertEqual(fit['iterations'], 500)
        with patch('orderbook.shape_curve_models.least_squares', side_effect=ValueError('private detail')):
            failed = analyze_curve_models(train, [], BookShapeConfig())
        fit = failed['spatial_cdf_models']['bid']['candidates'][0]
        self.assertEqual(fit['status'], 'failed')
        self.assertFalse(fit['converged'])
        self.assertIsNone(fit['parameters'])
        self.assertNotIn('private detail', json.dumps(failed))

    def test_empty_and_insufficient_support_have_no_fitted_claim(self):
        report = analyze_curve_models([], [], BookShapeConfig())
        self.assertEqual(report['status'], 'insufficient_calibration')
        self.assertEqual(report['spatial_cdf_models']['edges_bps'], [])
        self.assertEqual(report['spatial_cdf_models']['bid']['candidates'], [])
        self.assertEqual(report['slope_models']['bid']['calibration']['linear_slope_bps']['count'], 0)
        report = analyze_curve_models([row([1., 2.]) for _ in range(3)], [], BookShapeConfig(levels=2))
        self.assertEqual(report['status'], 'insufficient_test')
        self.assertTrue(all(fit['status'] == 'insufficient_support'
                            for fit in report['spatial_cdf_models']['bid']['candidates']))
        self.assertEqual(report['slope_models']['bid']['calibration']['quadratic_curvature_bps']['count'], 0)

    def test_examples_are_bounded_and_do_not_extrapolate_fraction_axis(self):
        train = [row(time=i, ask_scale=1.2) for i in range(8)]
        test = [row(time=i + 100) for i in range(8)]
        report = analyze_curve_models(train, test, BookShapeConfig())
        self.assertEqual(len(report['examples']['calibration']), 3)
        self.assertEqual([e['unix_us'] for e in report['examples']['calibration']], [0, 4, 7])
        for partition in ('calibration', 'test'):
            for example in report['examples'][partition]:
                for side in ('bid', 'ask'):
                    data = example[side]
                    self.assertEqual(data['fit_cumulative_fraction'][0], data['cumulative_fraction'][0])
                    self.assertEqual(data['fit_cumulative_fraction'][-1], data['cumulative_fraction'][-1])
                    self.assertEqual(len(data['linear_distance_bps']), 65)
                    self.assertEqual(len(data['quadratic_distance_bps']), 65)
                    self.assertEqual(data['fit_support_status'], 'available')
        json.dumps(report, allow_nan=False)

    def test_narrow_fraction_example_preserves_raw_points_without_inventing_fit_axis(self):
        rows = [row([1., 2., 3.], [1. - 2e-15, 1e-15, 1e-15], time=i) for i in range(3)]
        report = analyze_curve_models(rows, [], BookShapeConfig(levels=3))
        self.assertEqual(report['slope_models']['bid']['calibration']['linear_slope_bps']['count'], 0)
        self.assertEqual(len(report['examples']['calibration']), 3)
        for example in report['examples']['calibration']:
            for side in ('bid', 'ask'):
                data = example[side]
                np.testing.assert_allclose(data['distance_bps'], [1., 2., 3.])
                self.assertTrue(np.all(np.diff(data['cumulative_fraction']) > 0))
                self.assertEqual(data['fit_support_status'], 'numerically_unsupported_fraction_axis')
                self.assertEqual(data['fit_cumulative_fraction'], [])
                self.assertEqual(data['linear_distance_bps'], [])
                self.assertEqual(data['quadratic_distance_bps'], [])
        self.assertEqual(report['example_diagnostics']['calibration']['omitted_collapsed_fraction_states'], 0)
        json.dumps(report, allow_nan=False)

    def test_collapsed_fraction_example_omitted_but_features_and_cdf_preserved(self):
        rows = [row([1., 2., 3.], [1. - 2e-17, 1e-17, 1e-17], time=i) for i in range(3)]
        report = analyze_curve_models(rows, [], BookShapeConfig(levels=3))
        self.assertEqual(report['sample_counts']['calibration'], 3)
        self.assertEqual(report['examples']['calibration'], [])
        self.assertEqual(report['example_diagnostics']['calibration']['candidate_states'], 3)
        self.assertEqual(report['example_diagnostics']['calibration']['omitted_collapsed_fraction_states'], 3)
        self.assertEqual(report['spatial_cdf_models']['bid']['calibration']['samples'], 3)
        self.assertAlmostEqual(report['spatial_cdf_models']['bid']['calibration']['cdf_mean'][-1], 1.)
        np.testing.assert_array_equal(rows[0]['_bid_weights'], [1. - 2e-17, 1e-17, 1e-17])
        self.assertTrue(all(row['bid_curve_linear_slope_bps'] is None for row in rows))
        json.dumps(report, allow_nan=False)


if __name__ == '__main__':
    unittest.main()
