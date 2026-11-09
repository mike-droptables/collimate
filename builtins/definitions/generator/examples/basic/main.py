import gen
async def run():
    gen.update_file("hello.txt", "Hello")
if __name__ == "__main__":
    gen.main(run)
