package io.github.kianjross.herald.assistant;
interface IPrivilegedService {
    void destroy() = 16777114;
    String execute(String requestJson) = 0;
}
