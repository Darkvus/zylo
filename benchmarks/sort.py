def make(n):
    xs = []
    seed = 12345
    for i in range(n):
        seed = (seed * 1103515245 + 12345) % 2147483648
        xs.append(seed % 100000)
    return xs


def bubble_sort(xs):
    n = len(xs)
    for i in range(n):
        for j in range(n - 1 - i):
            if xs[j] > xs[j + 1]:
                xs[j], xs[j + 1] = xs[j + 1], xs[j]
    return xs


def main():
    data = bubble_sort(make(3000))
    print(data[0])
    print(data[1500])
    print(data[2999])


main()
