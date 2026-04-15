CC      := gcc
CFLAGS  := -std=c99 -Wall -Wextra -Isrc -MMD -MP
LDFLAGS :=

SRC_DIR := src
BUILD_DIR := bin
TARGET  := $(BUILD_DIR)/clox

SRCS := $(wildcard $(SRC_DIR)/*.c)
OBJS := $(patsubst $(SRC_DIR)/%.c,$(BUILD_DIR)/%.o,$(SRCS))
DEPS := $(OBJS:.o=.d)

RUN_ARGS := $(filter %.lox,$(MAKECMDGOALS))

.PHONY: all run clean

all: $(TARGET)

$(TARGET): $(OBJS)
	$(CC) $(LDFLAGS) -o $@ $^

$(BUILD_DIR)/%.o: $(SRC_DIR)/%.c | $(BUILD_DIR)
	$(CC) $(CFLAGS) -c -o $@ $<

$(BUILD_DIR):
	mkdir -p $(BUILD_DIR)

run: $(TARGET)
	./$(TARGET) $(RUN_ARGS)

%.lox:
	@:

clean:
	powershell -NoProfile -Command "if (Test-Path '$(BUILD_DIR)') { Remove-Item -Recurse -Force '$(BUILD_DIR)' }"

-include $(DEPS)
