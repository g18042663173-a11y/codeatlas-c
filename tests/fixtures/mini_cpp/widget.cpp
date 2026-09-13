#include "widget.hpp"

Widget::Widget() : value_(0) {}

Widget::~Widget() {}

void Widget::reset()
{
    value_ = 0;
}

int Widget::size() const
{
    return value_;
}

int Widget::value() const
{
    return value_;
}

int Widget::apply(int (*fn)(int), int value)
{
    return fn(value);
}

int Counter::size() const
{
    return Widget::size() + 1;
}

int helper(int x)
{
    return x + 1;
}
