fn is_prime(n: i64) -> bool {
    if n < 2 {
        return false;
    }
    let mut d = 2;
    while d * d <= n {
        if n % d == 0 {
            return false;
        }
        d += 1;
    }
    true
}

fn main() {
    let mut count = 0;
    for n in 1..300_000i64 {
        if is_prime(n) {
            count += 1;
        }
    }
    println!("{}", count);
}
