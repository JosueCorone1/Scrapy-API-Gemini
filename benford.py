"""Leading-digit Benford analysis and Pearson goodness-of-fit calculation."""
from decimal import Decimal, InvalidOperation
from math import exp, log10


DIGITS = tuple(range(1, 10))


def first_significant_digit(value):
    """Return the first nonzero base-10 digit or None for invalid input."""
    try:
        number = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not number.is_finite() or number <= 0:
        return None
    for character in format(number, "f"):
        if character in "123456789":
            return int(character)
    return None


def benford_proportions():
    """Theoretical first-significant-digit proportions for digits 1 to 9."""
    return {digit: log10(1 + 1 / digit) for digit in DIGITS}


def analyze_counts(values):
    """Compute the observed distribution, Pearson chi-square and p-value."""
    observed = {digit: 0 for digit in DIGITS}
    sample_size = 0
    for value in values:
        digit = first_significant_digit(value)
        if digit is not None:
            observed[digit] += 1
            sample_size += 1

    proportions = benford_proportions()
    expected = {digit: sample_size * proportions[digit] for digit in DIGITS}
    if sample_size == 0:
        return {
            "sample_size": 0,
            "observed": observed,
            "expected": expected,
            "proportions": proportions,
            "chi_square": None,
            "degrees_of_freedom": 8,
            "p_value": None,
        }

    chi_square = sum(
        (observed[digit] - expected[digit]) ** 2 / expected[digit]
        for digit in DIGITS
        if expected[digit] > 0
    )
    # Chi-square survival function with 8 degrees of freedom.
    x = chi_square / 2
    p_value = exp(-x) * (1 + x + x * x / 2 + x * x * x / 6)
    return {
        "sample_size": sample_size,
        "observed": observed,
        "expected": expected,
        "proportions": proportions,
        "chi_square": chi_square,
        "degrees_of_freedom": 8,
        "p_value": min(1.0, max(0.0, p_value)),
    }
