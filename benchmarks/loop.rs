fn main() {
    let mut total: i64 = 0;
    for i in 0..20_000_000i64 {
        total += i * i % 7;
    }
    println!("{}", total);
}
