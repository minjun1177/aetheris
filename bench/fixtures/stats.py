"""Small statistics helpers.

Plain Python on purpose: every function here takes a list of numbers and
returns a number or a list, with no third-party imports, so each one can be
read and checked on its own.
"""


def total(values):
    """The sum of `values`, 0 for an empty list."""
    result = 0
    for value in values:
        result += value
    return result


def mean(values):
    """The arithmetic mean. Raises ValueError for an empty list."""
    if not values:
        raise ValueError("mean of an empty list")
    return total(values) / len(values)


def minimum(values):
    """The smallest value. Raises ValueError for an empty list."""
    if not values:
        raise ValueError("minimum of an empty list")
    smallest = values[0]
    for value in values[1:]:
        if value < smallest:
            smallest = value
    return smallest


def maximum(values):
    """The largest value. Raises ValueError for an empty list."""
    if not values:
        raise ValueError("maximum of an empty list")
    largest = values[0]
    for value in values[1:]:
        if value > largest:
            largest = value
    return largest


def value_range(values):
    """The distance between the largest and the smallest value."""
    return maximum(values) - minimum(values)


def mode(values):
    """The most common value; the first one seen wins a tie."""
    if not values:
        raise ValueError("mode of an empty list")
    counts = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    best, best_count = values[0], 0
    for value in values:
        if counts[value] > best_count:
            best, best_count = value, counts[value]
    return best


def variance(values):
    """The population variance."""
    centre = mean(values)
    return total([(value - centre) ** 2 for value in values]) / len(values)


def geometric_mean(values):
    """The n-th root of the product. Every value must be positive."""
    if not values:
        raise ValueError("geometric mean of an empty list")
    product = 1.0
    for value in values:
        if value <= 0:
            raise ValueError("geometric mean needs positive values")
        product *= value
    return product ** (1 / len(values))


def harmonic_mean(values):
    """The reciprocal of the mean of the reciprocals."""
    if not values:
        raise ValueError("harmonic mean of an empty list")
    if any(value == 0 for value in values):
        raise ValueError("harmonic mean is undefined with a zero")
    return len(values) / total([1 / value for value in values])


def median(values):
    """The middle value of the sorted list.

    For an even number of values this is the mean of the two middle ones.
    Raises ValueError for an empty list.
    """
    if not values:
        raise ValueError("median of an empty list")
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return ordered[middle]
    return ordered[middle]


def standard_deviation(values):
    """The population standard deviation."""
    return variance(values) ** 0.5


def normalise(values):
    """Scale `values` into 0..1. A list of equal values becomes all zeros."""
    low, high = minimum(values), maximum(values)
    if high == low:
        return [0.0 for _ in values]
    return [(value - low) / (high - low) for value in values]


def cumulative(values):
    """Running totals: [1, 2, 3] becomes [1, 3, 6]."""
    running, result = 0, []
    for value in values:
        running += value
        result.append(running)
    return result


def moving_average(values, window):
    """The mean of each run of `window` consecutive values."""
    if window <= 0:
        raise ValueError("window must be positive")
    return [mean(values[start:start + window])
            for start in range(len(values) - window + 1)]


def percentile(values, fraction):
    """The value below which `fraction` (0..1) of the sorted values fall.

    Nearest-rank: no interpolation between neighbours.
    """
    if not values:
        raise ValueError("percentile of an empty list")
    if not 0 <= fraction <= 1:
        raise ValueError("fraction must be between 0 and 1")
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, round(fraction * len(ordered)) - 1))
    return ordered[index]


def z_scores(values):
    """How many standard deviations each value lies from the mean."""
    centre, spread = mean(values), standard_deviation(values)
    if spread == 0:
        return [0.0 for _ in values]
    return [(value - centre) / spread for value in values]


def histogram(values, buckets):
    """Counts of values in `buckets` equal-width bins across the range."""
    if buckets <= 0:
        raise ValueError("buckets must be positive")
    low, high = minimum(values), maximum(values)
    width = (high - low) / buckets or 1
    counts = [0] * buckets
    for value in values:
        index = min(buckets - 1, int((value - low) / width))
        counts[index] += 1
    return counts


def covariance(xs, ys):
    """The population covariance of two equally long lists."""
    if len(xs) != len(ys):
        raise ValueError("lists differ in length")
    mx, my = mean(xs), mean(ys)
    return total([(x - mx) * (y - my) for x, y in zip(xs, ys)]) / len(xs)


def correlation(xs, ys):
    """Pearson's correlation coefficient."""
    spread = standard_deviation(xs) * standard_deviation(ys)
    if spread == 0:
        raise ValueError("correlation of a constant list")
    return covariance(xs, ys) / spread


def describe(values):
    """The usual summary, as a dict."""
    return {
        "count": len(values),
        "mean": mean(values),
        "median": median(values),
        "min": minimum(values),
        "max": maximum(values),
        "stdev": standard_deviation(values),
    }
