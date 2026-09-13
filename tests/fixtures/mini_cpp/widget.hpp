#ifndef CODEATLAS_MINI_CPP_WIDGET_HPP
#define CODEATLAS_MINI_CPP_WIDGET_HPP

class Widget {
public:
    Widget();
    ~Widget();
    void reset();
    virtual int size() const;
    int apply(int (*fn)(int), int value);
    int value() const;

private:
    int value_;
};

class Counter : public Widget {
public:
    int size() const;
};

int helper(int x);

#endif
